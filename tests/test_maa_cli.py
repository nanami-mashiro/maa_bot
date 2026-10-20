import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from maa_tg_bot.android_login import AndroidLoginError
from maa_tg_bot.config import load_config
from maa_tg_bot.game_recovery import GameRecoveryError, RecoveryReport
from maa_tg_bot.maa_cli import MaaCliExecutor, ProcessResult
from maa_tg_bot.models import TaskKind, TaskRequest, TaskState


def test_config(tmp: Path | None = None):
    env = {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_ALLOWED_USER_IDS": "123",
        "MAA_AUTO_STARTUP": "false",
    }
    if tmp is not None:
        env["MAA_CONFIG_DIR"] = str(tmp / "maa-config")
        env["BOT_LOG_DIR"] = str(tmp / "logs")
    return load_config(
        path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
        env=env,
    )


class FakeLoginManager:
    def __init__(
        self,
        login_attempted: bool = False,
        error: AndroidLoginError | None = None,
    ):
        self.login_attempted = login_attempted
        self.error = error
        self.calls = 0

    async def login_if_needed(self) -> bool:
        self.calls += 1
        if self.error:
            raise self.error
        return self.login_attempted


class FakeGameRecovery:
    """Never touches a real device; returns scripted RecoveryReports or raises."""

    def __init__(self, reports=None):
        self.reports = list(reports or [])
        self.calls = 0

    async def recover(self, notify=None, *, stop_check=None):
        self.calls += 1
        if self.reports:
            item = self.reports.pop(0)
            if isinstance(item, Exception):
                raise item
            if notify is not None and not isinstance(item, Exception) and item.acted:
                await notify("progress")
            return item
        return RecoveryReport()


class TestExecutor(MaaCliExecutor):
    def __init__(
        self,
        config,
        login_manager,
        process_results=None,
        simple_results=None,
        game_recovery=None,
    ):
        super().__init__(
            config,
            login_manager=login_manager,
            game_recovery=game_recovery or FakeGameRecovery(),
        )
        self.process_results = list(process_results or [ProcessResult(exit_code=0, output="ok")])
        self.simple_results = list(simple_results or [])
        self.process_calls = []
        self.simple_calls = []

    async def ensure_adb(self) -> None:
        return None

    async def _run_process(self, command, timeout, log_path=None):
        self.process_calls.append((command, timeout, log_path))
        if not self.process_results:
            raise AssertionError("unexpected process call")
        return self.process_results.pop(0)

    async def _run_simple(self, command, timeout):
        self.simple_calls.append((command, timeout))
        if self.simple_results:
            return self.simple_results.pop(0)
        return ProcessResult(exit_code=0, output="")


class ClientUpdatingTestExecutor(TestExecutor):
    def __init__(
        self,
        config,
        login_manager,
        process_results=None,
        download_result=None,
        install_result=None,
        cleanup_error=None,
    ):
        super().__init__(config, login_manager, process_results=process_results)
        self.download_result = download_result or ProcessResult(exit_code=0, output="download ok")
        self.install_result = install_result or ProcessResult(exit_code=0, output="Success")
        self.cleanup_error = cleanup_error
        self.download_calls = 0
        self.install_calls = 0
        self.cleanup_calls = 0

    async def _download_client_apk(self):
        self.download_calls += 1
        return self.download_result

    async def _install_client_apk(self):
        self.install_calls += 1
        return self.install_result

    def _cleanup_client_apk(self):
        self.cleanup_calls += 1
        if self.cleanup_error:
            raise self.cleanup_error


class MaaCliExecutorTests(unittest.TestCase):
    def test_run_command_places_profile_after_subcommand(self):
        executor = MaaCliExecutor(test_config())

        command = executor._maa_command(["run", "tg_daily_abc123", "--batch"])

        self.assertEqual(command, ["maa", "run", "-p", "default", "tg_daily_abc123", "--batch"])

    def test_run_does_not_call_login_before_successful_maa_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            login_manager = FakeLoginManager()
            executor = TestExecutor(test_config(Path(tmp)), login_manager)
            request = TaskRequest(
                kind=TaskKind.FIGHT,
                requested_by=1,
                chat_id=1,
                options={"stage": "1-7"},
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(login_manager.calls, 0)
        self.assertEqual(len(executor.process_calls), 1)

    def test_auto_updates_maa_core_before_non_screenshot_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "false",
                    "MAA_CORE_UPDATE_ENABLED": "true",
                    "MAA_CORE_UPDATE_INTERVAL_SECONDS": "3600",
                    "MAA_CORE_UPDATE_TIMEOUT_SECONDS": "1200",
                    "MAA_CORE_UPDATE_TEST_TIME": "0",
                    "MAA_CORE_UPDATE_CHANNEL": "stable",
                },
            )
            executor = TestExecutor(
                env_config,
                FakeLoginManager(),
                simple_results=[ProcessResult(exit_code=0, output="updated")],
            )
            request = TaskRequest(
                kind=TaskKind.FIGHT,
                requested_by=1,
                chat_id=1,
                options={"stage": "1-7"},
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(
            executor.simple_calls[0],
            (["maa", "update", "--batch", "--test-time", "0", "stable"], 1200),
        )
        self.assertEqual(len(executor.process_calls), 1)

    def test_auto_update_failure_does_not_block_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "false",
                    "MAA_CORE_UPDATE_ENABLED": "true",
                },
            )
            executor = TestExecutor(
                env_config,
                FakeLoginManager(),
                simple_results=[ProcessResult(exit_code=1, output="network fail")],
            )
            request = TaskRequest(
                kind=TaskKind.FIGHT,
                requested_by=1,
                chat_id=1,
                options={"stage": "1-7"},
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(len(executor.simple_calls), 1)
        self.assertEqual(len(executor.process_calls), 1)

    def test_auto_update_is_skipped_inside_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "false",
                    "MAA_CORE_UPDATE_ENABLED": "true",
                    "MAA_CORE_UPDATE_INTERVAL_SECONDS": "3600",
                },
            )
            executor = TestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=0, output="first ok"),
                    ProcessResult(exit_code=0, output="second ok"),
                ],
                simple_results=[ProcessResult(exit_code=0, output="updated")],
            )
            first = TaskRequest(
                kind=TaskKind.FIGHT,
                requested_by=1,
                chat_id=1,
                options={"stage": "1-7"},
                id="abc123",
            )
            second = TaskRequest(
                kind=TaskKind.FIGHT,
                requested_by=1,
                chat_id=1,
                options={"stage": "1-7"},
                id="def456",
            )

            first_result = asyncio.run(executor.run(first))
            second_result = asyncio.run(executor.run(second))

        self.assertEqual(first_result.state, TaskState.SUCCEEDED)
        self.assertEqual(second_result.state, TaskState.SUCCEEDED)
        self.assertEqual(len(executor.simple_calls), 1)
        self.assertEqual(len(executor.process_calls), 2)

    def test_failed_task_without_login_screen_returns_original_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            login_manager = FakeLoginManager(login_attempted=False)
            executor = TestExecutor(
                test_config(Path(tmp)),
                login_manager,
                process_results=[ProcessResult(exit_code=1, output="maa failed")],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertIn("任务失败，退出码 1", result.message)
        self.assertEqual(result.output_tail, "maa failed")
        self.assertEqual(login_manager.calls, 1)
        self.assertEqual(len(executor.process_calls), 1)

    def test_daily_with_only_mail_award_error_is_treated_as_success_with_warning(self):
        output = """\
Summary
----------------------------------------
[领取邮件] 00:06:08 - 00:06:20 (12s) Error
----------------------------------------
[信用商店] 00:06:21 - 00:08:26 (2m 5s) Completed
----------------------------------------
[公开招募] 00:08:27 - 00:09:03 (35s) Completed
----------------------------------------
[刷理智] 00:09:03 - 00:14:31 (5m 28s) Completed
----------------------------------------
[基建换班] 00:14:32 - 00:23:21 (8m 49s) Completed
----------------------------------------
[领取任务奖励] 00:23:21 - 00:23:44 (22s) Completed
Error: Some error occurred during running task!
"""
        with tempfile.TemporaryDirectory() as tmp:
            login_manager = FakeLoginManager(login_attempted=False)
            executor = TestExecutor(
                test_config(Path(tmp)),
                login_manager,
                process_results=[ProcessResult(exit_code=1, output=output)],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("任务主体已完成", result.message)
        self.assertIn("领取邮件", result.message)
        self.assertEqual(result.output_tail, output)
        self.assertEqual(login_manager.calls, 0)
        self.assertEqual(len(executor.process_calls), 1)

    def test_failed_task_retries_after_login_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            login_manager = FakeLoginManager(login_attempted=True)
            executor = TestExecutor(
                test_config(Path(tmp)),
                login_manager,
                process_results=[
                    ProcessResult(exit_code=1, output="login screen"),
                    ProcessResult(exit_code=0, output="ok after login"),
                ],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(login_manager.calls, 1)
        self.assertEqual(len(executor.process_calls), 2)
        self.assertIn("已自动登录并重试", result.message)
        self.assertIn("tg_daily_abc123_login_retry.log", result.message)
        self.assertEqual(result.output_tail, "ok after login")

    def test_login_failure_after_maa_failure_returns_login_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            login_manager = FakeLoginManager(
                error=AndroidLoginError("bad login", output="verify failed")
            )
            executor = TestExecutor(
                test_config(Path(tmp)),
                login_manager,
                process_results=[ProcessResult(exit_code=1, output="maa failed")],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertIn("登录失败：bad login", result.message)
        self.assertEqual(result.output_tail, "verify failed")
        self.assertEqual(login_manager.calls, 1)
        self.assertEqual(len(executor.process_calls), 1)

    def test_startup_gate_runs_before_main_task_without_duplicate_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            executor = TestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=0, output="startup ok"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
            )
            request = TaskRequest(
                kind=TaskKind.FIGHT,
                requested_by=1,
                chat_id=1,
                options={"stage": "1-7"},
                id="abc123",
            )

            result = asyncio.run(executor.run(request))
            startup_task = Path(tmp) / "maa-config" / "tasks" / "tg_fight_abc123_startup_1.json"
            main_task = Path(tmp) / "maa-config" / "tasks" / "tg_fight_abc123.json"
            startup_doc = startup_task.read_text(encoding="utf-8")
            main_doc = main_task.read_text(encoding="utf-8")

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(
            [call[0][-2] for call in executor.process_calls],
            ["tg_fight_abc123_startup_1", "tg_fight_abc123"],
        )
        self.assertIn('"type": "StartUp"', startup_doc)
        self.assertNotIn('"type": "Fight"', startup_doc)
        self.assertIn('"type": "Fight"', main_doc)
        self.assertNotIn('"type": "StartUp"', main_doc)

    def test_startup_gate_retries_after_close_game_then_runs_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            executor = TestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=1, output="startup failed 1"),
                    ProcessResult(exit_code=1, output="startup failed 2"),
                    ProcessResult(exit_code=0, output="startup ok"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(
            [call[0][-2] for call in executor.process_calls],
            [
                "tg_daily_abc123_startup_1",
                "tg_daily_abc123_startup_2",
                "tg_daily_abc123_startup_3",
                "tg_daily_abc123",
            ],
        )
        force_stop_calls = [
            call
            for call, _timeout in executor.simple_calls
            if call[-3:] == ["am", "force-stop", "com.hypergryph.arknights"]
        ]
        self.assertEqual(len(force_stop_calls), 2)

    def test_startup_gate_fails_after_three_attempts_without_main_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            login_manager = FakeLoginManager()
            executor = TestExecutor(
                env_config,
                login_manager,
                process_results=[
                    ProcessResult(exit_code=1, output="startup failed 1"),
                    ProcessResult(exit_code=1, output="startup failed 2"),
                    ProcessResult(exit_code=1, output="startup failed 3"),
                ],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("启动游戏失败，已重试 3 次", result.message)
        self.assertIn("tg_daily_abc123_startup_3.log", result.message)
        self.assertEqual(result.output_tail, "startup failed 3")
        self.assertEqual(login_manager.calls, 3)
        self.assertEqual(
            [call[0][-2] for call in executor.process_calls],
            [
                "tg_daily_abc123_startup_1",
                "tg_daily_abc123_startup_2",
                "tg_daily_abc123_startup_3",
            ],
        )

    def test_startup_failure_on_login_screen_logs_in_and_retries_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            login_manager = FakeLoginManager(login_attempted=True)
            executor = TestExecutor(
                env_config,
                login_manager,
                process_results=[
                    ProcessResult(exit_code=1, output="startup failed"),
                    ProcessResult(exit_code=0, output="startup ok after login"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
            )
            request = TaskRequest(
                kind=TaskKind.FIGHT,
                requested_by=1,
                chat_id=1,
                options={"stage": "1-7"},
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(login_manager.calls, 1)
        self.assertIn("已自动登录并重试启动", result.message)
        self.assertEqual(
            [call[0][-2] for call in executor.process_calls],
            [
                "tg_fight_abc123_startup_1",
                "tg_fight_abc123_startup_after_login",
                "tg_fight_abc123",
            ],
        )
        force_stop_calls = [
            call
            for call, _timeout in executor.simple_calls
            if call[-3:] == ["am", "force-stop", "com.hypergryph.arknights"]
        ]
        self.assertEqual(force_stop_calls, [])

    def test_startup_gate_client_update_failure_installs_apk_and_retries_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            executor = ClientUpdatingTestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=0, output="startup ok after apk"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(executor.download_calls, 1)
        self.assertEqual(executor.install_calls, 1)
        self.assertEqual(executor.cleanup_calls, 1)
        self.assertIn("已自动更新客户端并重试启动", result.message)
        self.assertEqual(
            [call[0][-2] for call in executor.process_calls],
            [
                "tg_daily_abc123_startup_1",
                "tg_daily_abc123_startup_2",
                "tg_daily_abc123_startup_3",
                "tg_daily_abc123_startup_after_update",
                "tg_daily_abc123",
            ],
        )

    def test_startup_gate_plain_failure_does_not_install_apk(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            executor = ClientUpdatingTestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=1, output="startup failed 1"),
                    ProcessResult(exit_code=1, output="startup failed 2"),
                    ProcessResult(exit_code=1, output="startup failed 3"),
                ],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertEqual(executor.download_calls, 0)
        self.assertEqual(executor.install_calls, 0)

    def test_game_offline_is_not_treated_as_outdated_client(self):
        self.assertFalse(
            MaaCliExecutor._looks_like_client_update_required(
                "[INFO ] StartUp Start\n[WARN ] GameOffline\n[ERROR] StartUp Error"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "2",
                },
            )
            executor = ClientUpdatingTestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=1, output="[WARN ] GameOffline"),
                    ProcessResult(exit_code=1, output="[WARN ] GameOffline"),
                ],
            )
            request = TaskRequest(kind=TaskKind.DAILY, requested_by=1, chat_id=1, id="abc123")

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertEqual(executor.download_calls, 0)
        self.assertEqual(executor.install_calls, 0)

    def test_client_update_download_failure_returns_clear_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            executor = ClientUpdatingTestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                ],
                download_result=ProcessResult(exit_code=22, output="curl failed"),
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertEqual(executor.download_calls, 1)
        self.assertEqual(executor.install_calls, 0)
        self.assertEqual(executor.cleanup_calls, 0)
        self.assertIn("客户端自动更新失败：下载 APK 失败", result.message)
        self.assertEqual(result.output_tail, "curl failed")

    def test_client_update_install_failure_keeps_apk_for_debugging(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            executor = ClientUpdatingTestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                ],
                install_result=ProcessResult(exit_code=1, output="install failed"),
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertEqual(executor.download_calls, 1)
        self.assertEqual(executor.install_calls, 1)
        self.assertEqual(executor.cleanup_calls, 0)
        self.assertIn("客户端自动更新失败：安装 APK 失败", result.message)

    def test_client_update_cleanup_failure_does_not_fail_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            executor = ClientUpdatingTestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=1, output="客户端版本过低，请更新"),
                    ProcessResult(exit_code=0, output="startup ok after apk"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
                cleanup_error=OSError("unlink failed"),
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(executor.cleanup_calls, 1)
        self.assertIn("已自动更新客户端并重试启动", result.message)

    def test_startup_gate_cancelled_task_does_not_run_main_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                    "MAA_AUTO_STARTUP": "true",
                    "MAA_STARTUP_RETRIES": "3",
                },
            )
            executor = TestExecutor(
                env_config,
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=-15, output="stopped", cancelled=True),
                ],
            )
            request = TaskRequest(
                kind=TaskKind.DAILY,
                requested_by=1,
                chat_id=1,
                id="abc123",
            )

            result = asyncio.run(executor.run(request))

        self.assertEqual(result.state, TaskState.CANCELLED)
        self.assertEqual(result.message, "任务已取消。")
        self.assertEqual(len(executor.process_calls), 1)

    def test_close_game_force_stops_app_hides_keyboard_and_returns_home(self):
        executor = TestExecutor(test_config(), FakeLoginManager())

        result = asyncio.run(executor.close_game())

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            executor.simple_calls,
            [
                (
                    [
                        "adb",
                        "-s",
                        "redroid:5555",
                        "shell",
                        "am",
                        "force-stop",
                        "com.hypergryph.arknights",
                    ],
                    15,
                ),
                (
                    [
                        "adb",
                        "-s",
                        "redroid:5555",
                        "shell",
                        "input",
                        "keyevent",
                        "KEYCODE_BACK",
                    ],
                    15,
                ),
                (
                    [
                        "adb",
                        "-s",
                        "redroid:5555",
                        "shell",
                        "input",
                        "keyevent",
                        "KEYCODE_HOME",
                    ],
                    15,
                ),
            ],
        )

    def test_close_game_returns_failure_when_home_key_fails(self):
        executor = TestExecutor(
            test_config(),
            FakeLoginManager(),
            simple_results=[
                ProcessResult(exit_code=0, output=""),
                ProcessResult(exit_code=0, output=""),
                ProcessResult(exit_code=1, output="home failed"),
            ],
        )

        result = asyncio.run(executor.close_game())

        self.assertEqual(result.exit_code, 1)
        self.assertIn("返回桌面失败", result.output)
        self.assertIn("home failed", result.output)
        self.assertEqual(len(executor.simple_calls), 3)

    def test_close_game_stops_after_first_failed_step(self):
        executor = TestExecutor(
            test_config(),
            FakeLoginManager(),
            simple_results=[ProcessResult(exit_code=1, output="force stop failed")],
        )

        result = asyncio.run(executor.close_game())

        self.assertEqual(result.exit_code, 1)
        self.assertIn("关闭明日方舟失败", result.output)
        self.assertEqual(len(executor.simple_calls), 1)


class GameRecoveryHookTests(unittest.TestCase):
    def _config(self, tmp):
        return load_config(
            path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
            env={
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_ALLOWED_USER_IDS": "123",
                "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                "MAA_AUTO_STARTUP": "true",
                "MAA_STARTUP_RETRIES": "3",
            },
        )

    def _request(self, progress=None):
        return TaskRequest(
            kind=TaskKind.DAILY, requested_by=1, chat_id=1, id="abc123", progress=progress
        )

    def test_recovery_runs_before_startup_and_is_quiet_when_nothing_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            recovery = FakeGameRecovery()
            executor = TestExecutor(
                self._config(tmp),
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=0, output="startup ok"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
                game_recovery=recovery,
            )
            result = asyncio.run(executor.run(self._request()))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(recovery.calls, 1)
        self.assertNotIn("游戏自愈", result.message)

    def test_startup_failure_recovers_game_and_retries_immediately(self):
        sent = []

        async def progress(text):
            sent.append(text)

        with tempfile.TemporaryDirectory() as tmp:
            login = FakeLoginManager()
            recovery = FakeGameRecovery(
                [RecoveryReport(), RecoveryReport(relaunches=1, waited_update=True, update_seconds=600)]
            )
            executor = TestExecutor(
                self._config(tmp),
                login,
                process_results=[
                    ProcessResult(exit_code=1, output="GameOffline"),
                    ProcessResult(exit_code=0, output="startup ok"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
                game_recovery=recovery,
            )
            result = asyncio.run(executor.run(self._request(progress)))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(
            [call[0][-2] for call in executor.process_calls],
            [
                "tg_daily_abc123_startup_1",
                "tg_daily_abc123_startup_1_recovered",
                "tg_daily_abc123",
            ],
        )
        self.assertEqual(login.calls, 0)
        self.assertEqual(sent, ["progress"])
        self.assertIn("等待游戏资源更新约 10 分钟", result.message)
        self.assertIn("自动重开游戏 1 次", result.message)

    def test_progress_notify_failure_does_not_fail_task(self):
        async def broken(_text):
            raise RuntimeError("telegram down")

        with tempfile.TemporaryDirectory() as tmp:
            executor = TestExecutor(
                self._config(tmp),
                FakeLoginManager(),
                process_results=[
                    ProcessResult(exit_code=0, output="startup ok"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
                game_recovery=FakeGameRecovery([RecoveryReport(relaunches=1)]),
            )
            result = asyncio.run(executor.run(self._request(broken)))

        self.assertEqual(result.state, TaskState.SUCCEEDED)

    def test_recovery_error_fails_task_with_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = TestExecutor(
                self._config(tmp),
                FakeLoginManager(),
                process_results=[],
                game_recovery=FakeGameRecovery([GameRecoveryError("资源更新等待超时")]),
            )
            result = asyncio.run(executor.run(self._request()))

        self.assertEqual(result.state, TaskState.FAILED)
        self.assertIn("游戏自愈失败：资源更新等待超时", result.message)
        self.assertEqual(executor.process_calls, [])

    def test_main_task_failure_recovers_restarts_and_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            login = FakeLoginManager()
            recovery = FakeGameRecovery(
                [RecoveryReport(), RecoveryReport(relaunches=1), RecoveryReport()]
            )
            executor = TestExecutor(
                self._config(tmp),
                login,
                process_results=[
                    ProcessResult(exit_code=0, output="startup ok"),
                    ProcessResult(exit_code=1, output="network lost"),
                    ProcessResult(exit_code=0, output="startup ok again"),
                    ProcessResult(exit_code=0, output="main ok"),
                ],
                game_recovery=recovery,
            )
            result = asyncio.run(executor.run(self._request()))

        self.assertEqual(result.state, TaskState.SUCCEEDED)
        self.assertEqual(
            [call[0][-2] for call in executor.process_calls],
            [
                "tg_daily_abc123_startup_1",
                "tg_daily_abc123",
                "tg_daily_abc123_recovered_startup_1",
                "tg_daily_abc123",
            ],
        )
        self.assertEqual(login.calls, 0)
        self.assertIn("自动重开游戏 1 次", result.message)


class DrainUntilExitTests(unittest.TestCase):
    @staticmethod
    async def _spawn(*args):
        return await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    def test_returns_when_parent_exits_despite_inherited_pipe(self):
        # Regression for the event-loop freeze: the parent process exits immediately while a
        # backgrounded grandchild inherits stdout and keeps the pipe open. communicate() would
        # block until the grandchild exits (~30s); _drain_until_exit must return as soon as the
        # tracked process itself exits.
        async def scenario():
            proc = await self._spawn("sh", "-c", "echo hello; sleep 30 & exit 0")
            return await MaaCliExecutor._drain_until_exit(proc, timeout=60, drain_grace=1.0)

        start = time.monotonic()
        output, timed_out = asyncio.run(scenario())
        elapsed = time.monotonic() - start
        self.assertIn(b"hello", output)
        self.assertFalse(timed_out)
        self.assertLess(elapsed, 10)

    def test_times_out_and_kills_a_slow_process(self):
        async def scenario():
            proc = await self._spawn("sh", "-c", "echo busy; sleep 30")
            result = await MaaCliExecutor._drain_until_exit(proc, timeout=1, drain_grace=1.0)
            return result, proc.returncode

        start = time.monotonic()
        (output, timed_out), returncode = asyncio.run(scenario())
        elapsed = time.monotonic() - start
        self.assertTrue(timed_out)
        self.assertIsNotNone(returncode)
        self.assertLess(elapsed, 10)

    def test_normal_exit_captures_full_output(self):
        async def scenario():
            proc = await self._spawn("sh", "-c", "echo done")
            return await MaaCliExecutor._drain_until_exit(proc, timeout=10)

        output, timed_out = asyncio.run(scenario())
        self.assertEqual(output, b"done\n")
        self.assertFalse(timed_out)


class EnsureAdbReadyTests(unittest.TestCase):
    class ReadyExecutor(MaaCliExecutor):
        def __init__(self, config, boot_outputs):
            super().__init__(
                config,
                login_manager=FakeLoginManager(),
                game_recovery=FakeGameRecovery(),
            )
            self.boot_outputs = list(boot_outputs)
            self.simple_calls = []
            self.sleeps = []

        async def _run_simple(self, command, timeout):
            self.simple_calls.append(list(command))
            if "getprop" in command:
                output = self.boot_outputs.pop(0) if self.boot_outputs else ""
                return ProcessResult(exit_code=0, output=output)
            return ProcessResult(exit_code=0, output="connected")

        async def _ready_sleep(self, seconds):
            self.sleeps.append(seconds)

    def test_waits_for_boot_completed_with_backoff(self):
        executor = self.ReadyExecutor(test_config(), ["", "0\n", "1\n"])

        asyncio.run(executor.ensure_adb())

        self.assertEqual(executor.sleeps, [2.0, 4.0])
        getprops = [call for call in executor.simple_calls if "getprop" in call]
        self.assertEqual(len(getprops), 3)

    def test_raises_when_device_never_boots(self):
        executor = self.ReadyExecutor(test_config(), [])
        executor.ADB_READY_TIMEOUT_SECONDS = 0

        with self.assertRaises(RuntimeError):
            asyncio.run(executor.ensure_adb())


if __name__ == "__main__":
    unittest.main()
