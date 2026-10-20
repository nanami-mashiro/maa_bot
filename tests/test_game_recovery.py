import asyncio
import os
import unittest
from dataclasses import dataclass
from pathlib import Path

from maa_tg_bot.android_login import (
    DIALOG_DATA_EXPIRED,
    DIALOG_NETWORK_LOST,
    DIALOG_UNKNOWN,
    DIALOG_UPDATE_FAILED,
    classify_one_button_dialog,
    data_expired_dialog_visible,
)
from maa_tg_bot.config import load_config
from maa_tg_bot.game_recovery import (
    GameRecovery,
    GameRecoveryError,
    ScreenKind,
    ScreenState,
    classify_png,
    classify_screen,
)

# Text line boxes (top, bottom, left, right) measured on real 1280x720 screenshots.
LINES_207 = ((297, 320, 395, 885), (344, 367, 575, 702))
LINES_ERROR_0 = ((280, 303, 447, 833), (327, 353, 583, 696))
LINES_EXPIRED = ((320, 343, 472, 806),)


def dialog_screen(lines):
    """Synthetic one-button dialog: gray band, white text blocks, black strip, round button."""

    def pixel(x, y):
        if 620 <= x <= 660 and 470 <= y <= 512:
            return (20, 20, 20) if abs((x - 640) - (y - 491)) <= 3 else (255, 255, 255)
        for top, bottom, left, right in lines:
            # Glyph-like texture: white on every other column so the band stays visible.
            if top <= y <= bottom and left <= x <= right and (x % 2 == 0 or x in (left, right)):
                return (240, 240, 240)
        if 165 <= y <= 455:
            return (52, 52, 52)
        if 456 <= y <= 530:
            return (12, 12, 12)
        return (188, 188, 188)

    return pixel


def update_screen(fill_x, digits=0):
    """Synthetic resource update screen: art, black strip with text, yellow/gray bar."""

    def pixel(x, y):
        if y < 628:
            return (90, 70, 60)
        if 694 <= y <= 696:
            return (255, 216, 0) if x <= fill_x else (49, 49, 49)
        if 654 <= y <= 671 and 470 <= x <= 797 and x % 3 == 0:
            # A different "MB" number changes which columns are lit.
            if 700 <= x <= 760 and (x // 3 + digits) % 2:
                return (0, 0, 0)
            return (230, 230, 230)
        return (0, 0, 0)

    return pixel


def scaled(pixel, width, height):
    return lambda x, y: pixel(round(x * 1280 / width), round(y * 720 / height))


class ScreenClassificationTests(unittest.TestCase):
    def test_dialog_kinds_by_text_geometry(self):
        self.assertEqual(classify_one_button_dialog(1280, 720, dialog_screen(LINES_207)), DIALOG_UPDATE_FAILED)
        self.assertEqual(classify_one_button_dialog(1280, 720, dialog_screen(LINES_ERROR_0)), DIALOG_NETWORK_LOST)
        self.assertEqual(classify_one_button_dialog(1280, 720, dialog_screen(LINES_EXPIRED)), DIALOG_DATA_EXPIRED)
        self.assertEqual(classify_one_button_dialog(1280, 720, dialog_screen(())), DIALOG_UNKNOWN)

    def test_dialog_kinds_scale_to_1080p(self):
        pixel = scaled(dialog_screen(LINES_207), 1920, 1080)
        self.assertEqual(classify_one_button_dialog(1920, 1080, pixel), DIALOG_UPDATE_FAILED)
        pixel = scaled(dialog_screen(LINES_ERROR_0), 1920, 1080)
        self.assertEqual(classify_screen(1920, 1080, pixel).kind, ScreenKind.NETWORK_LOST)

    def test_error_number_length_change_still_matches(self):
        lines = (LINES_207[0], (344, 367, 565, 712))  # e.g. 「错误号:2071」
        self.assertEqual(classify_one_button_dialog(1280, 720, dialog_screen(lines)), DIALOG_UPDATE_FAILED)

    def test_data_expired_detection_ignores_207_and_network_lost(self):
        self.assertFalse(data_expired_dialog_visible(1280, 720, dialog_screen(LINES_207)))
        self.assertFalse(data_expired_dialog_visible(1280, 720, dialog_screen(LINES_ERROR_0)))
        self.assertTrue(data_expired_dialog_visible(1280, 720, dialog_screen(LINES_EXPIRED)))

    def test_update_screen_progress_signature(self):
        first = classify_screen(1280, 720, update_screen(900, digits=0))
        same = classify_screen(1280, 720, update_screen(900, digits=0))
        moved = classify_screen(1280, 720, update_screen(900, digits=1))
        self.assertEqual(first.kind, ScreenKind.UPDATING)
        self.assertEqual(first.percent, 71)
        self.assertEqual(first.progress, same.progress)
        self.assertNotEqual(first.progress, moved.progress)
        self.assertEqual(classify_screen(1280, 720, update_screen(0)).percent, 0)

    def test_other_screens(self):
        self.assertEqual(classify_screen(1280, 720, lambda x, y: (200, 180, 160)).kind, ScreenKind.OTHER)
        self.assertEqual(classify_screen(1280, 720, lambda x, y: (0, 0, 0)).kind, ScreenKind.OTHER)
        self.assertEqual(classify_screen(1280, 1024, update_screen(500)).kind, ScreenKind.OTHER)
        self.assertEqual(classify_png(b"").kind, ScreenKind.UNAVAILABLE)
        self.assertEqual(classify_png(b"not a png").kind, ScreenKind.UNAVAILABLE)


@unittest.skipUnless(os.environ.get("MAA_SCREEN_FIXTURES"), "set MAA_SCREEN_FIXTURES=<dir of real screenshots>")
class RealScreenshotTests(unittest.TestCase):
    """Optional check against real redroid screenshots (not committed; too large)."""

    EXPECTED = {
        "s16.png": ScreenKind.UPDATE_FAILED,
        "s18.png": ScreenKind.UPDATE_FAILED,
        "s21.png": ScreenKind.NETWORK_LOST,
        "s20.png": ScreenKind.UPDATING,
        "s17.png": ScreenKind.UPDATING,
        "data_expired_dialog.png": ScreenKind.DATA_EXPIRED,
        "maa_mainmenu.png": ScreenKind.OTHER,
        "e3.png": ScreenKind.OTHER,
    }

    def test_real_screens(self):
        root = Path(os.environ["MAA_SCREEN_FIXTURES"])
        checked = 0
        for name, kind in self.EXPECTED.items():
            path = root / name
            if path.exists():
                checked += 1
                self.assertEqual(classify_png(path.read_bytes()).kind, kind, name)
        self.assertGreater(checked, 0)


@dataclass
class Result:
    exit_code: int | None
    output: str = ""


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def recovery_config(**env):
    base = {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_USER_IDS": "123"}
    base.update(env)
    return load_config(path=Path("/tmp/nonexistent-maa-tg-bot.toml"), env=base)


class ScriptedRecovery(GameRecovery):
    def __init__(self, states, *, pidof=None, config=None, monkey_result=None):
        self.clock = FakeClock()
        self.commands = []
        self.states = list(states)
        self.pidof = list(pidof or [])
        self.monkey_result = monkey_result or Result(0, "Events injected: 1")
        super().__init__(
            config or recovery_config(),
            self.run_simple,
            capture_png=self.no_capture,
            sleep=self.clock.sleep,
            clock=self.clock,
        )

    async def no_capture(self):
        return b""

    async def detect(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return state() if callable(state) else state

    async def run_simple(self, command, timeout):
        args = list(command[4:])
        self.commands.append(args)
        if args[0] == "pidof":
            pids = self.pidof.pop(0) if self.pidof else ""
            return Result(0 if pids else 1, pids)
        if args[0] == "monkey":
            return self.monkey_result
        if args[:3] == ["cmd", "package", "resolve-activity"]:
            return Result(0, "priority=0\ncom.hypergryph.arknights/com.u8.sdk.U8UnityContext\n")
        return Result(0)


OTHER = ScreenState(ScreenKind.OTHER)
FAILED_207 = ScreenState(ScreenKind.UPDATE_FAILED)
LOST_0 = ScreenState(ScreenKind.NETWORK_LOST)


def updating(sig, percent=50):
    return ScreenState(ScreenKind.UPDATING, progress=(sig, "x"), percent=percent)


class RecoveryLoopTests(unittest.TestCase):
    def run_recover(self, recovery, notify_log=None, notify=None):
        async def default_notify(text):
            notify_log.append(text)

        return asyncio.run(recovery.recover(notify or (default_notify if notify_log is not None else None)))

    def test_nothing_wrong_returns_immediately_without_adb(self):
        recovery = ScriptedRecovery([OTHER])
        report = self.run_recover(recovery)
        self.assertFalse(report.acted)
        self.assertEqual(recovery.commands, [])
        self.assertEqual(recovery.clock.now, 0)

    def test_207_dialog_force_stops_and_relaunches(self):
        sent = []
        recovery = ScriptedRecovery([FAILED_207, OTHER])
        report = self.run_recover(recovery, sent)
        self.assertEqual(report.relaunches, 1)
        self.assertEqual(
            recovery.commands,
            [
                ["am", "force-stop", "com.hypergryph.arknights"],
                ["pidof", "com.hypergryph.arknights"],
                ["monkey", "-p", "com.hypergryph.arknights", "-c", "android.intent.category.LAUNCHER", "1"],
            ],
        )
        self.assertEqual(len(sent), 1)
        self.assertIn("207", sent[0])
        self.assertIn("1/6", sent[0])
        # never clears data
        self.assertFalse(any("clear" in " ".join(c) or "rm" in c for c in recovery.commands))

    def test_network_lost_dialog_relaunches(self):
        sent = []
        report = self.run_recover(ScriptedRecovery([LOST_0, OTHER]), sent)
        self.assertEqual(report.relaunches, 1)
        self.assertIn("连接丢失", sent[0])

    def test_stale_pid_is_killed(self):
        recovery = ScriptedRecovery([FAILED_207, OTHER], pidof=["25547", ""])
        self.run_recover(recovery)
        self.assertIn(["kill", "-9", "25547"], recovery.commands)

    def test_unkillable_pid_raises(self):
        recovery = ScriptedRecovery([FAILED_207, OTHER], pidof=["25547", "25547"])
        with self.assertRaises(GameRecoveryError):
            self.run_recover(recovery)

    def test_monkey_failure_falls_back_to_am_start(self):
        recovery = ScriptedRecovery([FAILED_207, OTHER], monkey_result=Result(1, "error"))
        self.run_recover(recovery)
        self.assertIn(
            ["am", "start", "-n", "com.hypergryph.arknights/com.u8.sdk.U8UnityContext"],
            recovery.commands,
        )

    def test_waits_for_update_with_progress_then_reports_done(self):
        sent = []
        states = [updating(i) for i in range(30)] + [OTHER]
        recovery = ScriptedRecovery(states)
        report = self.run_recover(recovery, sent)
        self.assertTrue(report.waited_update)
        self.assertEqual(report.relaunches, 0)
        self.assertEqual(recovery.commands, [])
        self.assertEqual(len(sent), 2)
        self.assertIn("正在更新资源", sent[0])
        self.assertIn("更新已完成", sent[1])
        self.assertGreaterEqual(report.update_seconds, 30 * 120)

    def test_stalled_update_relaunches_after_stall_window(self):
        sent = []
        # progress stops moving: same signature for > 15 minutes, then moves again.
        states = [updating(1)] * 9 + [updating(2), OTHER]
        recovery = ScriptedRecovery(states)
        report = self.run_recover(recovery, sent)
        self.assertEqual(report.relaunches, 1)
        self.assertTrue(any("没有变化" in text for text in sent))
        self.assertIn(["am", "force-stop", "com.hypergryph.arknights"], recovery.commands)

    def test_relaunch_cap_raises(self):
        recovery = ScriptedRecovery([FAILED_207])
        with self.assertRaises(GameRecoveryError) as ctx:
            self.run_recover(recovery)
        self.assertIn("6 次", str(ctx.exception))
        self.assertEqual(sum(1 for c in recovery.commands if c[:2] == ["am", "force-stop"]), 6)

    def test_update_timeout_raises(self):
        counter = iter(range(10_000))
        recovery = ScriptedRecovery(
            [lambda: updating(next(counter))],
            config=recovery_config(GAME_RECOVERY_UPDATE_TIMEOUT_SECONDS="3600"),
        )
        with self.assertRaises(GameRecoveryError) as ctx:
            self.run_recover(recovery)
        self.assertIn("60 分钟", str(ctx.exception))

    def test_notify_failure_is_swallowed(self):
        async def broken(_text):
            raise RuntimeError("telegram down")

        report = self.run_recover(ScriptedRecovery([FAILED_207, OTHER]), notify=broken)
        self.assertEqual(report.relaunches, 1)

    def test_stop_check_aborts(self):
        class Stop(Exception):
            pass

        def stop():
            raise Stop

        recovery = ScriptedRecovery([updating(1)])
        with self.assertRaises(Stop):
            asyncio.run(recovery.recover(None, stop_check=stop))

    def test_disabled(self):
        recovery = ScriptedRecovery([FAILED_207], config=recovery_config(GAME_RECOVERY_ENABLED="false"))
        self.assertFalse(self.run_recover(recovery).acted)
        self.assertEqual(recovery.commands, [])

    def test_settles_through_splash_before_returning(self):
        # After a relaunch the game shows splash screens before the update screen
        # resumes; recovery must not return on the first calm screenshot.
        sent = []
        states = [FAILED_207, OTHER, updating(1), updating(2), OTHER, OTHER, OTHER]
        recovery = ScriptedRecovery(states)
        report = self.run_recover(recovery, sent)
        self.assertEqual(report.relaunches, 1)
        self.assertTrue(report.waited_update)
        self.assertIn("更新已完成", sent[-1])


if __name__ == "__main__":
    unittest.main()
