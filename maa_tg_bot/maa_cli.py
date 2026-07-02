from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .android_login import AndroidLoginError, AndroidLoginManager
from .config import AppConfig
from .maa_summary import parse_maa_summary
from .models import TaskKind, TaskRequest, TaskResult, TaskState
from .task_builder import MaaTaskBuilder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    output: str
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class StartupGateResult:
    result: ProcessResult
    attempts: int
    task_path: Path
    log_path: Path


@dataclass(frozen=True)
class ClientUpdateRecovery:
    startup: StartupGateResult | None = None
    failure_message: str = ""
    failure_result: ProcessResult | None = None


CLIENT_UPDATE_REQUIRED_PATTERNS = (
    "GameOffline",
    "ClientVersion",
    "client version",
    "version update",
    "版本过低",
    "客户端版本",
    "客户端过期",
    "强制更新",
    "请更新",
    "停服更新",
)
TOLERATED_DAILY_ERROR_LABELS = {"领取邮件"}
SUCCESSFUL_SUMMARY_STATUSES = {"Completed", "Skipped"}
FAILED_SUMMARY_STATUSES = {"Error", "Failed"}


def tail_text(value: str, max_chars: int = 4000) -> str:
    if len(value) <= max_chars:
        return value
    return value[-max_chars:]


class TaskStopRequested(Exception):
    pass


class MaaCliExecutor:
    def __init__(
        self,
        config: AppConfig,
        login_manager: AndroidLoginManager | None = None,
    ):
        self.config = config
        self.builder = MaaTaskBuilder(config)
        self._process: asyncio.subprocess.Process | None = None
        self._stop_requested = False
        self._stop_event = asyncio.Event()
        self._core_update_lock = asyncio.Lock()
        self._last_core_update_attempt_at: float | None = None
        self.login_manager = login_manager or AndroidLoginManager(
            config,
            self._run_simple,
        )

    async def run(self, request: TaskRequest) -> TaskResult:
        started_at = datetime.now(UTC)
        if request.kind == TaskKind.SCREENSHOT:
            return await self._run_screenshot(request, started_at)

        try:
            self._stop_requested = False
            self._stop_event.clear()
            await self._maybe_update_maa_core()
            await self.ensure_adb()
            task_name = f"tg_{request.kind.value}_{request.id}"
            recovered_client_update = False
            if self.config.maa.auto_startup:
                startup = await self._run_startup_gate(task_name, request)
                if startup.result.cancelled:
                    raise TaskStopRequested
                if startup.result.exit_code != 0:
                    recovery = await self._try_client_update_recovery(task_name, request, startup)
                    if recovery is not None:
                        if recovery.startup is None:
                            return TaskResult(
                                request=request,
                                state=TaskState.FAILED,
                                started_at=started_at,
                                finished_at=datetime.now(UTC),
                                exit_code=(
                                    recovery.failure_result.exit_code
                                    if recovery.failure_result is not None
                                    else startup.result.exit_code
                                ),
                                message=recovery.failure_message,
                                output_tail=tail_text(
                                    recovery.failure_result.output
                                    if recovery.failure_result is not None
                                    else startup.result.output
                                ),
                            )
                        if recovery.startup.result.cancelled:
                            raise TaskStopRequested
                        startup = recovery.startup
                        recovered_client_update = startup.result.exit_code == 0
                    if startup.result.exit_code != 0:
                        return TaskResult(
                            request=request,
                            state=TaskState.FAILED,
                            started_at=started_at,
                            finished_at=datetime.now(UTC),
                            exit_code=startup.result.exit_code,
                            message=self._startup_failure_message(startup),
                            output_tail=tail_text(startup.result.output),
                        )
            self._raise_if_stop_requested()
            task_path = self._write_task_file(task_name, request, include_startup=False)
            log_path = self._log_path(task_name)
            command = self._maa_command(["run", task_name, "--batch"])
            result = await self._run_process(
                command,
                timeout=self.config.maa.task_timeout_seconds,
                log_path=log_path,
            )
            retried_after_login = False
            tolerated_daily_errors = self._tolerated_daily_error_labels(request, result)
            if result.exit_code != 0 and not result.cancelled and not tolerated_daily_errors:
                login_attempted = await self.login_manager.login_if_needed()
                if login_attempted:
                    retried_after_login = True
                    log_path = self._log_path(f"{task_name}_login_retry")
                    result = await self._run_process(
                        command,
                        timeout=self.config.maa.task_timeout_seconds,
                        log_path=log_path,
                    )
                    tolerated_daily_errors = self._tolerated_daily_error_labels(request, result)
            state = (
                TaskState.SUCCEEDED
                if result.exit_code == 0 or tolerated_daily_errors
                else TaskState.FAILED
            )
            if result.cancelled:
                state = TaskState.CANCELLED
            if tolerated_daily_errors and state == TaskState.SUCCEEDED:
                message = self._tolerated_daily_failure_message(
                    result,
                    task_path,
                    log_path,
                    tolerated_daily_errors,
                )
            else:
                message = self._task_message(result, task_path, log_path)
            if recovered_client_update:
                message = f"已自动更新客户端并重试启动。{message}"
            if retried_after_login:
                message = f"已自动登录并重试。{message}"
            return TaskResult(
                request=request,
                state=state,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=result.exit_code,
                message=message,
                output_tail=tail_text(result.output),
            )
        except TaskStopRequested:
            return TaskResult(
                request=request,
                state=TaskState.CANCELLED,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                message="任务已取消。",
            )
        except AndroidLoginError as exc:
            return TaskResult(
                request=request,
                state=TaskState.FAILED,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                message=f"登录失败：{exc}",
                output_tail=tail_text(exc.output),
            )
        except Exception as exc:
            return TaskResult(
                request=request,
                state=TaskState.FAILED,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                message=f"执行器异常：{exc}",
            )
        finally:
            if self._process is None:
                self._stop_requested = False
            self._stop_event.clear()

    async def health(self) -> dict[str, str]:
        version = await self._run_simple([self.config.maa.bin, "version"], timeout=10)
        adb = await self.adb_status()
        return {
            "maa": self._health_line(version),
            "adb": self._health_line(adb),
            "config_dir": str(self.config.maa.config_dir),
            "profile": self.config.maa.profile,
        }

    async def adb_status(self) -> ProcessResult:
        try:
            await self.ensure_adb()
        except Exception as exc:
            return ProcessResult(exit_code=1, output=str(exc))
        return await self._run_simple(
            [self.config.android.adb_bin, "-s", self.config.android.adb_serial, "get-state"],
            timeout=10,
        )

    async def _maybe_update_maa_core(self) -> None:
        update = self.config.maa_update
        if not update.enabled:
            return
        if self._core_update_in_interval():
            return

        async with self._core_update_lock:
            if self._core_update_in_interval():
                return
            self._last_core_update_attempt_at = datetime.now(UTC).timestamp()
            command = [
                self.config.maa.bin,
                "update",
                "--batch",
                "--test-time",
                str(update.test_time),
                update.channel,
            ]
            logger.info("静默检查并更新 MaaCore/资源 channel=%s", update.channel)
            result = await self._run_simple(command, timeout=update.timeout_seconds)
            if result.exit_code == 0:
                output = tail_text(result.output.strip(), 1000)
                logger.info("MaaCore/资源静默更新完成%s", f"：{output}" if output else "")
                return
            logger.warning(
                "MaaCore/资源静默更新失败，继续执行任务。退出码=%s，输出=%s",
                self._process_code(result),
                tail_text(result.output.strip(), 1000),
            )

    def _core_update_in_interval(self) -> bool:
        update = self.config.maa_update
        if self._last_core_update_attempt_at is None:
            return False
        elapsed = datetime.now(UTC).timestamp() - self._last_core_update_attempt_at
        return elapsed < update.interval_seconds

    async def close_game(self) -> ProcessResult:
        try:
            await self.ensure_adb()
        except Exception as exc:
            return ProcessResult(exit_code=1, output=str(exc))
        steps = [
            (
                "关闭明日方舟",
                [
                    self.config.android.adb_bin,
                    "-s",
                    self.config.android.adb_serial,
                    "shell",
                    "am",
                    "force-stop",
                    self.config.login.package_name,
                ],
            ),
            (
                "收起输入法",
                [
                    self.config.android.adb_bin,
                    "-s",
                    self.config.android.adb_serial,
                    "shell",
                    "input",
                    "keyevent",
                    "KEYCODE_BACK",
                ],
            ),
            (
                "返回桌面",
                [
                    self.config.android.adb_bin,
                    "-s",
                    self.config.android.adb_serial,
                    "shell",
                    "input",
                    "keyevent",
                    "KEYCODE_HOME",
                ],
            ),
        ]
        outputs: list[str] = []
        for label, command in steps:
            result = await self._run_simple(command, timeout=15)
            if result.output.strip():
                outputs.append(result.output.strip())
            if result.exit_code != 0:
                output = f"{label}失败。"
                if result.output.strip():
                    output += f"\n{result.output.strip()}"
                return ProcessResult(
                    exit_code=result.exit_code,
                    output=output,
                    timed_out=result.timed_out,
                )
        return ProcessResult(exit_code=0, output="\n".join(outputs))

    async def _run_startup_gate(
        self,
        task_name: str,
        request: TaskRequest,
    ) -> StartupGateResult:
        attempts = max(1, self.config.maa.startup_retries)
        last: StartupGateResult | None = None
        for attempt in range(1, attempts + 1):
            self._raise_if_stop_requested()
            startup_task_name = f"{task_name}_startup_{attempt}"
            task_path = self._write_task_doc(
                startup_task_name,
                self.builder.build_startup(request.options),
            )
            log_path = self._log_path(startup_task_name)
            logger.info("启动游戏尝试 %s/%s", attempt, attempts)
            result = await self._run_process(
                self._maa_command(["run", startup_task_name, "--batch"]),
                timeout=self.config.maa.task_timeout_seconds,
                log_path=log_path,
            )
            last = StartupGateResult(
                result=result,
                attempts=attempt,
                task_path=task_path,
                log_path=log_path,
            )
            if result.cancelled or result.exit_code == 0:
                return last
            logger.warning("启动游戏尝试 %s/%s 失败，退出码=%s", attempt, attempts, result.exit_code)
            if attempt < attempts:
                close_result = await self.close_game()
                if close_result.exit_code != 0:
                    logger.warning(
                        "启动游戏重试前关闭游戏失败，退出码=%s，输出=%s",
                        close_result.exit_code,
                        tail_text(close_result.output, 1000),
                    )
        if last is None:
            raise RuntimeError("启动游戏重试次数配置异常")
        return last

    async def _try_client_update_recovery(
        self,
        task_name: str,
        request: TaskRequest,
        startup: StartupGateResult,
    ) -> ClientUpdateRecovery | None:
        if not self.config.client_update.enabled:
            return None
        if not self._looks_like_client_update_required(startup.result.output):
            return None

        logger.warning("启动失败疑似客户端过期，开始自动下载并安装明日方舟客户端")
        download_result = await self._download_client_apk()
        if download_result.exit_code != 0:
            return ClientUpdateRecovery(
                failure_message=(
                    "客户端自动更新失败：下载 APK 失败。"
                    f"退出码={self._process_code(download_result)}"
                ),
                failure_result=download_result,
            )

        install_result = await self._install_client_apk()
        if install_result.exit_code != 0:
            return ClientUpdateRecovery(
                failure_message=(
                    "客户端自动更新失败：安装 APK 失败。"
                    f"退出码={self._process_code(install_result)}"
                ),
                failure_result=install_result,
            )

        try:
            self._cleanup_client_apk()
        except OSError as exc:
            logger.warning("客户端 APK 安装成功，但删除安装包失败：%s", exc)

        close_result = await self.close_game()
        if close_result.exit_code != 0:
            logger.warning(
                "客户端自动更新后关闭游戏失败，退出码=%s，输出=%s",
                close_result.exit_code,
                tail_text(close_result.output, 1000),
            )

        self._raise_if_stop_requested()
        startup_task_name = f"{task_name}_startup_after_update"
        task_path = self._write_task_doc(
            startup_task_name,
            self.builder.build_startup(request.options),
        )
        log_path = self._log_path(startup_task_name)
        logger.info("客户端自动更新完成，重试启动游戏")
        startup_result = await self._run_process(
            self._maa_command(["run", startup_task_name, "--batch"]),
            timeout=self.config.maa.task_timeout_seconds,
            log_path=log_path,
        )
        return ClientUpdateRecovery(
            startup=StartupGateResult(
                result=startup_result,
                attempts=startup.attempts + 1,
                task_path=task_path,
                log_path=log_path,
            )
        )

    async def _download_client_apk(self) -> ProcessResult:
        cache_dir = self.config.maa.config_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        apk_path = self._client_apk_path()
        tmp_path = apk_path.with_suffix(".apk.part")
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        command = [
            "curl",
            "-fL",
            "--retry",
            "2",
            "--connect-timeout",
            "30",
            "-o",
            str(tmp_path),
            self.config.client_update.download_url,
        ]
        result = await self._run_simple(
            command,
            timeout=self.config.client_update.download_timeout_seconds,
        )
        if result.exit_code != 0:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            return result
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            return ProcessResult(exit_code=1, output="下载完成但 APK 文件不存在或为空")
        tmp_path.replace(apk_path)
        return result

    async def _install_client_apk(self) -> ProcessResult:
        await self.ensure_adb()
        apk_path = self._client_apk_path()
        if not apk_path.exists():
            return ProcessResult(exit_code=1, output=f"APK 文件不存在：{apk_path}")
        return await self._run_simple(
            [
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "install",
                "-r",
                str(apk_path),
            ],
            timeout=self.config.client_update.install_timeout_seconds,
        )

    def _client_apk_path(self) -> Path:
        return self.config.maa.config_dir / "cache" / "arknights-official.apk"

    def _cleanup_client_apk(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._client_apk_path().unlink()

    @staticmethod
    def _looks_like_client_update_required(output: str) -> bool:
        normalized = output.lower()
        return any(pattern.lower() in normalized for pattern in CLIENT_UPDATE_REQUIRED_PATTERNS)

    def _raise_if_stop_requested(self) -> None:
        if self._stop_requested or self._stop_event.is_set():
            raise TaskStopRequested

    async def ensure_adb(self) -> None:
        if not self.config.android.adb_connect_host:
            return
        result = await self._run_simple(
            [self.config.android.adb_bin, "connect", self.config.android.adb_connect_host],
            timeout=15,
        )
        if result.exit_code != 0:
            raise RuntimeError(f"ADB 连接失败：{tail_text(result.output, 1000)}")

    async def stop_current(self) -> bool:
        self._stop_requested = True
        self._stop_event.set()
        process = self._process
        if process is None or process.returncode is not None:
            return False
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
        return True

    def _write_task_file(
        self,
        task_name: str,
        request: TaskRequest,
        *,
        include_startup: bool | None = None,
    ) -> Path:
        return self._write_task_doc(
            task_name,
            self.builder.build(
                request.kind,
                request.options,
                include_startup=include_startup,
            ),
        )

    def _write_task_doc(self, task_name: str, task_doc: dict) -> Path:
        task_dir = self.config.maa.config_dir / "tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        task_path = task_dir / f"{task_name}.json"
        task_path.write_text(json.dumps(task_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return task_path

    def _log_path(self, task_name: str) -> Path:
        self.config.bot.log_dir.mkdir(parents=True, exist_ok=True)
        return self.config.bot.log_dir / f"{task_name}.log"

    def _maa_command(self, args: Sequence[str]) -> list[str]:
        if args and args[0] == "run":
            command = [self.config.maa.bin, "run"]
            if self.config.maa.profile:
                command.extend(["-p", self.config.maa.profile])
            command.extend(args[1:])
            return command
        return [self.config.maa.bin, *args]

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["MAA_CONFIG_DIR"] = str(self.config.maa.config_dir)
        env["MAA_LOG"] = self.config.maa.log_level
        env["NO_COLOR"] = "1"
        return env

    @staticmethod
    async def _drain_until_exit(
        process: asyncio.subprocess.Process,
        timeout: int,
        *,
        drain_grace: float = 5.0,
    ) -> tuple[bytes, bool]:
        """Return (stdout_bytes, timed_out).

        Reliable even when a child inherits the stdout pipe: waits on the process itself
        (process.wait()) rather than on stdout EOF, while draining stdout concurrently so the
        pipe buffer never fills. After exit, waits at most ``drain_grace`` for buffered
        output, then gives up instead of blocking on a still-open inherited fd.
        """
        chunks: list[bytes] = []

        async def _drain() -> None:
            if process.stdout is None:
                return
            with contextlib.suppress(Exception):
                while True:
                    chunk = await process.stdout.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)

        drain_task = asyncio.create_task(_drain())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=10)
        # Process has exited / been killed. Let the drain flush briefly, then stop waiting
        # even if a grandchild still holds the pipe open.
        try:
            await asyncio.wait_for(asyncio.shield(drain_task), timeout=drain_grace)
        except TimeoutError:
            drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await drain_task
        return b"".join(chunks), timed_out

    async def _run_process(
        self,
        command: Sequence[str],
        timeout: int,
        log_path: Path | None = None,
    ) -> ProcessResult:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._env(),
        )
        self._process = process
        try:
            stdout, timed_out = await self._drain_until_exit(process, timeout)
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            if log_path:
                log_path.write_text(output, encoding="utf-8")
            if timed_out:
                logger.warning(
                    "进程超时被终止：command=%s timeout=%ss", command[0], timeout
                )
                return ProcessResult(
                    exit_code=process.returncode,
                    output=output,
                    timed_out=True,
                )
            return ProcessResult(
                exit_code=process.returncode,
                output=output,
                cancelled=self._stop_requested,
            )
        finally:
            self._process = None
            self._stop_requested = False

    async def _run_simple(self, command: Sequence[str], timeout: int) -> ProcessResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._env(),
            )
        except FileNotFoundError as exc:
            return ProcessResult(exit_code=127, output=str(exc))
        stdout, timed_out = await self._drain_until_exit(process, timeout)
        if timed_out:
            return ProcessResult(exit_code=None, output="命令超时", timed_out=True)
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        return ProcessResult(exit_code=process.returncode, output=output)

    async def _run_screenshot(self, request: TaskRequest, started_at: datetime) -> TaskResult:
        try:
            await self.ensure_adb()
            self.config.bot.log_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = self.config.bot.log_dir / f"screenshot_{request.id}.png"
            command = [
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "exec-out",
                "screencap",
                "-p",
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.android.screenshot_timeout_seconds,
            )
            if process.returncode != 0:
                output = stderr.decode("utf-8", errors="replace")
                return TaskResult(
                    request=request,
                    state=TaskState.FAILED,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    exit_code=process.returncode,
                    message="截图失败",
                    output_tail=tail_text(output),
                )
            screenshot_path.write_bytes(stdout)
            return TaskResult(
                request=request,
                state=TaskState.SUCCEEDED,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=0,
                message="截图已完成",
                artifact_path=screenshot_path,
            )
        except Exception as exc:
            return TaskResult(
                request=request,
                state=TaskState.FAILED,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                message=f"截图异常：{exc}",
            )

    @staticmethod
    def _tolerated_daily_error_labels(
        request: TaskRequest,
        result: ProcessResult,
    ) -> tuple[str, ...]:
        if request.kind != TaskKind.DAILY or result.exit_code == 0 or result.timed_out:
            return ()

        items = parse_maa_summary(result.output)
        if not items:
            return ()

        failed_labels: list[str] = []
        has_successful_core_item = False
        for item in items:
            if item.status in SUCCESSFUL_SUMMARY_STATUSES:
                if item.label not in TOLERATED_DAILY_ERROR_LABELS:
                    has_successful_core_item = True
                continue
            if item.status in FAILED_SUMMARY_STATUSES and item.label in TOLERATED_DAILY_ERROR_LABELS:
                failed_labels.append(item.label)
                continue
            return ()

        if not failed_labels or not has_successful_core_item:
            return ()
        return tuple(dict.fromkeys(failed_labels))

    @staticmethod
    def _tolerated_daily_failure_message(
        result: ProcessResult,
        task_path: Path,
        log_path: Path,
        labels: Sequence[str],
    ) -> str:
        return (
            "任务主体已完成，非关键步骤失败："
            f"{'、'.join(labels)}。原始退出码 {result.exit_code}。"
            f"task={task_path} log={log_path}"
        )

    @staticmethod
    def _task_message(result: ProcessResult, task_path: Path, log_path: Path) -> str:
        if result.cancelled:
            return f"任务已取消。task={task_path} log={log_path}"
        if result.timed_out:
            return f"任务超时。task={task_path} log={log_path}"
        if result.exit_code == 0:
            return f"任务已完成。task={task_path} log={log_path}"
        return f"任务失败，退出码 {result.exit_code}。task={task_path} log={log_path}"

    @staticmethod
    def _process_code(result: ProcessResult) -> str:
        return "超时" if result.timed_out else str(result.exit_code)

    @staticmethod
    def _startup_failure_message(startup: StartupGateResult) -> str:
        code = "超时" if startup.result.timed_out else str(startup.result.exit_code)
        return (
            f"启动游戏失败，已重试 {startup.attempts} 次。退出码={code} "
            f"task={startup.task_path} log={startup.log_path}"
        )

    @staticmethod
    def _health_line(result: ProcessResult) -> str:
        text = tail_text(result.output.strip(), 500)
        code = "超时" if result.timed_out else str(result.exit_code)
        return f"退出码={code} {text}".strip()
