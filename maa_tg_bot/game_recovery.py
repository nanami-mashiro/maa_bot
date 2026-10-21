"""Self-healing for game states that MAA StartUp cannot get past on its own.

Handled (all observed on redroid with the official CN client):

* 「更新下载失败，请检查网络连接后确认重试 错误号:207」 during the in-game resource
  update. The downloader thread is dead, so the in-game retry does nothing.
* 「与神经网络连接丢失，请重新连接 (错误号:0)」 where confirming does nothing.
* A resource update (「正在释放神经递质 (xxxMB/yyyMB)」 + progress bar) that is
  still running: wait for it instead of failing StartUp, and relaunch the game
  when progress stalls.

Recovery is always ``am force-stop`` + kill any stale game PID + relaunch. The
game resumes the download from its partial files. App data/cache is never
cleared. The game is drawn by Unity, so screens are recognized from pixels on
an ``adb exec-out screencap`` like android_login does.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
import time
import zlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from .android_login import (
    DIALOG_DATA_EXPIRED,
    DIALOG_NETWORK_LOST,
    DIALOG_UNKNOWN,
    DIALOG_UPDATE_FAILED,
    PixelAt,
    RunSimple,
    adb_screencap_png,
    classify_one_button_dialog,
    png_pixel_at,
    scale_point,
)
from .config import AppConfig

logger = logging.getLogger(__name__)

ProgressNotify = Callable[[str], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]


class ScreenKind(str, Enum):
    UPDATE_FAILED = "update_failed"
    NETWORK_LOST = "network_lost"
    DATA_EXPIRED = "data_expired"
    UNKNOWN_DIALOG = "unknown_dialog"
    UPDATING = "updating"
    OTHER = "other"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ScreenState:
    kind: ScreenKind
    # Progress signature while updating: changes whenever the MB counter text or
    # the progress bar fill changes.
    progress: tuple[int, str] | None = None
    percent: int | None = None


# Update screen (1280x720 reference layout): a black strip at the bottom with
# the 「正在释放神经递质 (xxxMB/yyyMB)」 text, and a 3px progress bar at y=694..696
# that is yellow (filled) or dark gray (track) across the full width.
UPDATE_STRIP_ROWS = (634, 645, 684, 690)
UPDATE_STRIP_COLUMNS = (40, 200, 360, 920, 1080, 1240)
UPDATE_BAR_ROW = 695
UPDATE_BAR_EDGE_ROWS = (691, 699)
UPDATE_BAR_COLUMNS = tuple(range(8, 1273, 24))
UPDATE_TEXT_BOX = (466, 648, 806, 678)  # left, top, right, bottom; excludes the spinner


def _is_bar_fill(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red >= 230 and 190 <= green <= 235 and blue <= 50


def _is_bar_track(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return 36 <= red <= 64 and abs(red - green) < 8 and abs(green - blue) < 8


def _is_black(pixel: tuple[int, int, int]) -> bool:
    return max(pixel) <= 16


def update_progress(width: int, height: int, pixel_at: PixelAt) -> tuple[tuple[int, str], int] | None:
    """Return ((bar_fill_x, text_hash), percent) on the resource update screen, else None."""
    if width <= 0 or height <= 0 or abs(width / height - 16 / 9) > 0.02:
        return None

    def px(ref_x: int, ref_y: int) -> tuple[int, int, int]:
        x, y = scale_point((ref_x, ref_y), width, height)
        return pixel_at(min(max(x, 0), width - 1), min(max(y, 0), height - 1))

    strip_points = [(x, y) for y in UPDATE_STRIP_ROWS for x in UPDATE_STRIP_COLUMNS]
    if sum(_is_black(px(x, y)) for x, y in strip_points) < len(strip_points) - 1:
        return None
    bar = [px(x, UPDATE_BAR_ROW) for x in UPDATE_BAR_COLUMNS]
    if sum(_is_bar_fill(p) or _is_bar_track(p) for p in bar) < len(bar) * 0.9:
        return None
    edges = [px(x, y) for y in UPDATE_BAR_EDGE_ROWS for x in UPDATE_BAR_COLUMNS]
    if sum(_is_black(p) for p in edges) < len(edges) * 0.9:
        return None

    left, top, right, bottom = UPDATE_TEXT_BOX
    digest = hashlib.sha1()
    bright = 0
    for ref_y in range(top, bottom + 1):
        row = bytearray()
        for ref_x in range(left, right + 1):
            on = min(px(ref_x, ref_y)) >= 150
            bright += on
            row.append(1 if on else 0)
        digest.update(bytes(row))
    if bright < 20:
        return None

    fill_x = 0
    for ref_x in range(0, 1280, 4):
        if _is_bar_fill(px(ref_x, UPDATE_BAR_ROW)):
            fill_x = ref_x
    percent = round(fill_x * 100 / 1276) if fill_x else 0
    return (fill_x, digest.hexdigest()[:16]), min(percent, 100)


def classify_screen(width: int, height: int, pixel_at: PixelAt) -> ScreenState:
    dialog = classify_one_button_dialog(width, height, pixel_at)
    if dialog == DIALOG_UPDATE_FAILED:
        return ScreenState(ScreenKind.UPDATE_FAILED)
    if dialog == DIALOG_NETWORK_LOST:
        return ScreenState(ScreenKind.NETWORK_LOST)
    if dialog == DIALOG_DATA_EXPIRED:
        return ScreenState(ScreenKind.DATA_EXPIRED)
    if dialog == DIALOG_UNKNOWN:
        return ScreenState(ScreenKind.UNKNOWN_DIALOG)
    progress = update_progress(width, height, pixel_at)
    if progress is not None:
        signature, percent = progress
        return ScreenState(ScreenKind.UPDATING, progress=signature, percent=percent)
    return ScreenState(ScreenKind.OTHER)


def classify_png(png: bytes) -> ScreenState:
    if not png:
        return ScreenState(ScreenKind.UNAVAILABLE)
    try:
        width, height, pixel_at = png_pixel_at(png)
    except (ValueError, zlib.error, struct.error, IndexError) as exc:
        logger.warning("解析截图失败，跳过游戏状态识别：%s", exc)
        return ScreenState(ScreenKind.UNAVAILABLE)
    return classify_screen(width, height, pixel_at)


DAILY_RESET_UTC_OFFSET_HOURS = 8
DAILY_RESET_HOUR = 4
CLOCK_TICKS_PER_SECOND = 100


def last_daily_reset_epoch(now: float) -> float:
    """Epoch seconds of the most recent 04:00 UTC+8 server reset at or before now."""
    shift = (DAILY_RESET_UTC_OFFSET_HOURS - DAILY_RESET_HOUR) * 3600
    return ((int(now) + shift) // 86400) * 86400 - shift


def parse_process_start_epoch(output: str) -> tuple[float | None, float | None]:
    """Parse ``cat /proc/PID/stat /proc/uptime; date +%s`` into (start epoch, now)."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) < 3:
        return None, None
    try:
        stat_fields = lines[0].rsplit(")", 1)[1].split()
        start_ticks = int(stat_fields[19])  # field 22 of /proc/PID/stat
        uptime = float(lines[1].split()[0])
        now = float(lines[2])
    except (IndexError, ValueError):
        return None, None
    return now - uptime + start_ticks / CLOCK_TICKS_PER_SECOND, now


class GameRecoveryError(RuntimeError):
    pass


@dataclass
class RecoveryReport:
    relaunches: int = 0
    waited_update: bool = False
    update_seconds: float = 0.0

    @property
    def acted(self) -> bool:
        return self.relaunches > 0 or self.waited_update


RELAUNCH_REASONS = {
    ScreenKind.UPDATE_FAILED: "资源更新卡在「更新下载失败 错误号:207」",
    ScreenKind.NETWORK_LOST: "出现「与神经网络连接丢失（错误号:0）」",
}


class GameRecovery:
    """Detect stuck game screens and heal them by relaunching the game."""

    def __init__(
        self,
        config: AppConfig,
        run_simple: RunSimple,
        *,
        capture_png: Callable[[], Awaitable[bytes]] | None = None,
        sleep: SleepFunc = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.settings = config.recovery
        self._run_simple = run_simple
        self._capture_png = capture_png or self._default_capture
        self._sleep = sleep
        self._clock = clock

    async def _default_capture(self) -> bytes:
        return await adb_screencap_png(self.config.android.adb_bin, self.config.android.adb_serial)

    async def detect(self) -> ScreenState:
        png = await self._capture_png()
        # Pure-Python PNG decoding takes ~1s; keep the Telegram event loop responsive.
        return await asyncio.to_thread(classify_png, png)

    async def recover(
        self,
        notify: ProgressNotify | None = None,
        *,
        stop_check: Callable[[], None] | None = None,
    ) -> RecoveryReport:
        """Heal the current game screen; return quickly when nothing is wrong.

        Raises GameRecoveryError when the update never finishes within
        update_timeout_seconds or relaunches exceed max_relaunches.
        """
        report = RecoveryReport()
        if not self.settings.enabled:
            return report
        settings = self.settings
        start = self._clock()
        deadline = start + settings.update_timeout_seconds
        update_started: float | None = None
        last_signature: tuple[int, str] | None = None
        last_progress_at = start
        settle_checks = 0

        async def notify_safe(text: str) -> None:
            if notify is None:
                return
            try:
                await asyncio.wait_for(notify(text), timeout=90)
            except Exception:
                logger.warning("游戏自愈进度通知发送失败：%s", text, exc_info=True)

        async def relaunch(reason: str) -> None:
            nonlocal last_signature, last_progress_at, settle_checks
            if report.relaunches >= settings.max_relaunches:
                raise GameRecoveryError(
                    f"{reason}，已自动重开游戏 {report.relaunches} 次仍未恢复，放弃本次任务"
                )
            report.relaunches += 1
            logger.warning("%s，自动重开游戏（第 %s/%s 次）", reason, report.relaunches, settings.max_relaunches)
            await self.relaunch_game()
            await notify_safe(
                f"{reason}，已自动关闭并重开游戏（第 {report.relaunches}/{settings.max_relaunches} 次）。"
            )
            last_signature = None
            last_progress_at = self._clock()
            settle_checks = 0
            await self._sleep(settings.relaunch_wait_seconds)

        while True:
            if stop_check is not None:
                stop_check()
            state = await self.detect()
            now = self._clock()

            if state.kind in RELAUNCH_REASONS:
                await relaunch(RELAUNCH_REASONS[state.kind])
                continue

            if state.kind == ScreenKind.UPDATING:
                settle_checks = 0
                if update_started is None:
                    update_started = now
                    report.waited_update = True
                    logger.info("检测到游戏正在更新资源（%s%%），等待完成", state.percent)
                    await notify_safe(
                        f"游戏正在更新资源（进度约 {state.percent}%），等下载完成后再启动，"
                        f"最长等待 {settings.update_timeout_seconds // 3600} 小时。"
                    )
                if state.progress != last_signature:
                    last_signature = state.progress
                    last_progress_at = now
                elif now - last_progress_at >= settings.stall_seconds:
                    await relaunch(
                        f"资源更新进度 {round((now - last_progress_at) / 60)} 分钟没有变化（{state.percent}%）"
                    )
                    continue
                if now >= deadline:
                    raise GameRecoveryError(
                        f"游戏资源更新等待超过 {settings.update_timeout_seconds // 60} 分钟仍未完成"
                        f"（进度约 {state.percent}%）"
                    )
                await self._sleep(settings.poll_seconds)
                continue

            # Not stuck and not updating. Right after a relaunch or an update the
            # game passes splash/loading screens before the update screen comes
            # back, so only trust "done" after a few consecutive calm checks.
            if not report.acted:
                return report
            settle_checks += 1
            if settle_checks >= settings.settle_checks:
                if update_started is not None:
                    report.update_seconds = now - update_started
                    await notify_safe(
                        f"游戏资源更新已完成（等待约 {max(1, round(report.update_seconds / 60))} 分钟），继续启动。"
                    )
                logger.info(
                    "游戏自愈结束：重开 %s 次，等待更新=%s", report.relaunches, report.waited_update
                )
                return report
            if now >= deadline:
                raise GameRecoveryError("游戏自愈等待超时")
            await self._sleep(settings.settle_seconds)

    async def relaunch_game(self) -> None:
        """Force-stop the game, kill a leftover PID if any, then start it again."""
        package = self.config.login.package_name
        await self._adb_shell(["am", "force-stop", package], timeout=20)
        await self._sleep(2)
        pids = await self._game_pids(package)
        if pids:
            logger.warning("force-stop 后仍有游戏进程 %s，强制结束", " ".join(pids))
            await self._adb_shell(["kill", "-9", *pids], timeout=15)
            await self._sleep(2)
            pids = await self._game_pids(package)
            if pids:
                raise GameRecoveryError(f"无法结束残留的游戏进程：{' '.join(pids)}")
        await self.launch_game()

    async def launch_game(self) -> None:
        """Start the game and make sure a process actually appears.

        ``am start -n <launcher activity>`` first: on redroid ``monkey`` aborts
        with "SYS_KEYS has no physical keys" yet exits 0 without starting the
        game, so monkey is only a fallback.
        """
        package = self.config.login.package_name
        attempts: list[str] = []
        activity = await self._launch_activity(package)
        if activity:
            started = await self._adb_shell(["am", "start", "-n", activity], timeout=30)
            attempts.append(started.output.strip()[-200:])
            if started.exit_code == 0 and "Error" not in started.output and await self._wait_for_game_pid(package):
                return
        result = await self._adb_shell(
            ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=30,
        )
        attempts.append(result.output.strip()[-200:])
        if await self._wait_for_game_pid(package):
            return
        raise GameRecoveryError(f"重开游戏失败：{' | '.join(a for a in attempts if a)[-300:]}")

    async def _wait_for_game_pid(self, package: str, checks: int = 5) -> bool:
        for _ in range(checks):
            await self._sleep(2)
            if await self._game_pids(package):
                return True
        return False

    async def restart_if_open_across_reset(self) -> bool:
        """Relaunch the game when it has been running since before the last daily reset.

        The CN server resets at 04:00 (UTC+8). A game left open across it shows
        「数据文件已过期，请重新登录」 at the first server call, so every MAA task
        fails with GameOffline. Returns True when it relaunched.
        """
        package = self.config.login.package_name
        pids = await self._game_pids(package)
        if not pids:
            return False
        started_at, now = await self._process_start_epoch(pids[0])
        if started_at is None or now is None:
            return False
        last_reset = last_daily_reset_epoch(now)
        if started_at >= last_reset:
            return False
        logger.warning(
            "游戏进程 %s 启动于上一次 04:00 数据刷新之前（%.1f 小时前），先重开游戏",
            pids[0],
            (now - started_at) / 3600,
        )
        await self.relaunch_game()
        await self._sleep(self.settings.relaunch_wait_seconds)
        return True

    async def _process_start_epoch(self, pid: str) -> tuple[float | None, float | None]:
        result = await self._adb_shell(
            ["cat", f"/proc/{pid}/stat", "/proc/uptime", ";", "date", "+%s"], timeout=10
        )
        return parse_process_start_epoch(result.output) if result.exit_code == 0 else (None, None)

    async def _game_pids(self, package: str) -> list[str]:
        result = await self._adb_shell(["pidof", package], timeout=10)
        if result.exit_code != 0:
            return []
        return [token for token in result.output.split() if token.isdigit()]

    async def _launch_activity(self, package: str) -> str:
        result = await self._adb_shell(
            ["cmd", "package", "resolve-activity", "--brief", package], timeout=15
        )
        lines = [line.strip() for line in result.output.splitlines() if "/" in line]
        return lines[-1] if result.exit_code == 0 and lines else ""

    async def _adb_shell(self, args: Sequence[str], timeout: int):
        return await self._run_simple(
            [
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "shell",
                *args,
            ],
            timeout=timeout,
        )
