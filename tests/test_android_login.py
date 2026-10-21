import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from maa_tg_bot.android_login import (
    AndroidLoginError,
    AndroidLoginManager,
    account_login_button_visible,
    data_expired_dialog_visible,
    start_title_visible,
    png_pixel_at,
)
from maa_tg_bot.config import load_config


EMPTY_UI_XML = """\
<hierarchy rotation="0">
  <node bounds="[0,0][1280,720]" />
</hierarchy>
"""

PHONE_LOGIN_UI_XML = """\
<hierarchy rotation="0">
  <node text="密码登录" bounds="[775,522][849,602]" />
</hierarchy>
"""

PASSWORD_LOGIN_UI_XML = """\
<hierarchy rotation="0">
  <node resource-id="com.hypergryph.arknights:id/hg_login_view_edit_text_input_account" bounds="[488,241][780,301]" />
  <node resource-id="com.hypergryph.arknights:id/hg_login_view_edit_text_input_password" bounds="[488,321][780,381]" />
  <node resource-id="com.hypergryph.arknights:id/hg_login_view_check_box_check_agreement" bounds="[431,399][468,436]" />
  <node resource-id="com.hypergryph.arknights:id/hg_login_view_button_password_login" bounds="[431,478][849,538]" />
</hierarchy>
"""


@dataclass
class FakeResult:
    exit_code: int | None
    output: str = ""


class FakeRunner:
    def __init__(self):
        self.simple_calls: list[tuple[list[str], int]] = []

    async def run_simple(self, command, timeout):
        self.simple_calls.append((list(command), timeout))
        return FakeResult(0)


class TestLoginManager(AndroidLoginManager):
    def __init__(self, *args, **kwargs):
        ui_xmls = kwargs.pop("ui_xmls", None)
        self.title_button_visible = kwargs.pop("title_button_visible", False)
        self.form_closes_on_login = kwargs.pop("form_closes_on_login", True)
        self.data_expired_visible = kwargs.pop("data_expired_visible", None)
        # list of results returned one per call (last one repeats), or a constant
        self.start_title = kwargs.pop("start_title", False)
        self.saved_screenshots: list[str] = []
        super().__init__(
            *args,
            sleep=self.no_sleep,
            **kwargs,
        )
        self.shell_scripts: list[str] = []
        self.ui_xmls = list(ui_xmls or [PASSWORD_LOGIN_UI_XML])

    async def _title_account_button_visible(self):
        return self.title_button_visible

    async def _data_expired_dialog_visible(self):
        return self.data_expired_visible

    async def _start_title_visible(self):
        if isinstance(self.start_title, list):
            return self.start_title.pop(0) if len(self.start_title) > 1 else self.start_title[0]
        return self.start_title

    async def _save_debug_screenshot(self, tag: str) -> None:
        self.saved_screenshots.append(tag)

    async def _tap(self, x: int, y: int) -> None:
        await super()._tap(x, y)
        if self.form_closes_on_login and (x, y) in {(640, 508), (640, 498)}:
            self.ui_xmls = [EMPTY_UI_XML]

    async def no_sleep(self, _seconds: float) -> None:
        return None

    async def _run_adb_shell_script(self, script: str, *, timeout: int, error_message: str) -> None:
        self.shell_scripts.append(script)

    async def _dump_ui_xml(self) -> str:
        if len(self.ui_xmls) > 1:
            return self.ui_xmls.pop(0)
        if self.ui_xmls:
            return self.ui_xmls[0]
        return ""


def test_config(tmp: Path):
    return load_config(
        path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
            "ARKNIGHTS_LOGIN_ENABLED": "true",
            "ARKNIGHTS_LOGIN_USERNAME": "doctor@example.com",
            "ARKNIGHTS_LOGIN_PASSWORD": "password123",
            "MAA_CONFIG_DIR": str(tmp / "maa-config"),
            "BOT_LOG_DIR": str(tmp / "logs"),
        },
    )


class AndroidLoginManagerTests(unittest.TestCase):
    def test_does_not_login_when_ui_is_not_login_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML],
            )

            logged_in = asyncio.run(manager.login_if_needed())

        self.assertFalse(logged_in)
        self.assertEqual(manager.shell_scripts, [])
        tap_calls = [call for call in runner.simple_calls if "tap" in call[0]]
        self.assertEqual(tap_calls, [])

    def test_logs_in_when_password_form_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[PASSWORD_LOGIN_UI_XML],
            )

            logged_in = asyncio.run(manager.login_if_needed())

        self.assertTrue(logged_in)
        tap_calls = [call for call in runner.simple_calls if "tap" in call[0]]
        self.assertGreaterEqual(len(tap_calls), 4)
        self.assertIn("634", tap_calls[-4][0])
        self.assertIn("351", tap_calls[-3][0])
        self.assertIn("449", tap_calls[-2][0])
        self.assertIn("640", tap_calls[-1][0])
        commands = [call[0] for call in runner.simple_calls]
        back_indexes = [index for index, command in enumerate(commands) if "KEYCODE_BACK" in command]
        password_index = next(index for index, command in enumerate(commands) if "351" in command)
        self.assertGreaterEqual(len(back_indexes), 2)
        self.assertLess(back_indexes[0], password_index)
        self.assertTrue(any("doctor@example.com" in script for script in manager.shell_scripts))
        self.assertTrue(any("password123" in script for script in manager.shell_scripts))
        self.assertTrue(
            any(
                "KEYCODE_MOVE_END" in script and "while" not in script
                for script in manager.shell_scripts
            )
        )

    def test_does_not_login_when_feature_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(
                path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
                env={
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_IDS": "123",
                    "MAA_CONFIG_DIR": str(Path(tmp) / "maa-config"),
                    "BOT_LOG_DIR": str(Path(tmp) / "logs"),
                },
            )
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[PASSWORD_LOGIN_UI_XML],
            )

            logged_in = asyncio.run(manager.login_if_needed())

        self.assertFalse(logged_in)
        self.assertEqual(runner.simple_calls, [])
        self.assertEqual(manager.shell_scripts, [])

    def test_switches_from_phone_login_to_password_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML, PHONE_LOGIN_UI_XML, PASSWORD_LOGIN_UI_XML],
            )

            asyncio.run(manager._open_password_login_form())

            tap_calls = [call for call in runner.simple_calls if "tap" in call[0]]
            self.assertEqual([call[0][-2:] for call in tap_calls], [["812", "562"]])

    def test_waits_for_password_form_without_retapping_title_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML] * 6 + [PASSWORD_LOGIN_UI_XML],
            )

            asyncio.run(manager._open_password_login_form())

            tap_calls = [call for call in runner.simple_calls if "tap" in call[0]]
            self.assertEqual(tap_calls, [])

    def test_does_not_toggle_agreement_when_already_checked(self):
        checked_ui = PASSWORD_LOGIN_UI_XML.replace(
            'hg_login_view_check_box_check_agreement" bounds="[431,399][468,436]"',
            'hg_login_view_check_box_check_agreement" checked="true" bounds="[431,399][468,436]"',
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[checked_ui],
            )

            logged_in = asyncio.run(manager.login_if_needed())

        self.assertTrue(logged_in)
        tap_calls = [call for call in runner.simple_calls if "tap" in call[0]]
        self.assertFalse(any("449" in call[0] for call in tap_calls))

    def test_logs_in_from_unity_title_account_button(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML, PASSWORD_LOGIN_UI_XML],
                title_button_visible=True,
            )

            logged_in = asyncio.run(manager.login_if_needed())

        self.assertTrue(logged_in)
        tap_calls = [call for call in runner.simple_calls if "tap" in call[0]]
        self.assertEqual(tap_calls[0][0][-2:], ["640", "512"])
        self.assertEqual(sum(call[0][-2:] == ["640", "512"] for call in tap_calls), 1)
        self.assertTrue(any("doctor@example.com" in script for script in manager.shell_scripts))

    def test_title_tap_scaled_to_screen_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML, PASSWORD_LOGIN_UI_XML],
                title_button_visible=(1920, 1080),
            )

            asyncio.run(manager.login_if_needed())

        tap_calls = [call for call in runner.simple_calls if "tap" in call[0]]
        self.assertEqual(tap_calls[0][0][-2:], ["960", "768"])

    def test_never_types_credentials_when_form_does_not_appear(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML],
                title_button_visible=True,
            )

            with self.assertRaises(AndroidLoginError):
                asyncio.run(manager.login_if_needed())

        self.assertEqual(manager.shell_scripts, [])
        tap_calls = [call for call in runner.simple_calls if "tap" in call[0]]
        self.assertEqual([call[0][-2:] for call in tap_calls], [["640", "512"]])

    def test_stops_before_password_when_form_closes_after_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            # open form, confirm before account, tap account; then the form is gone
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[PASSWORD_LOGIN_UI_XML] * 3 + [EMPTY_UI_XML],
            )

            with self.assertRaises(AndroidLoginError):
                asyncio.run(manager._perform_official_login())

        self.assertTrue(any("doctor@example.com" in script for script in manager.shell_scripts))
        self.assertFalse(any("password123" in script for script in manager.shell_scripts))

    def test_raises_when_login_form_stays_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[PASSWORD_LOGIN_UI_XML],
                form_closes_on_login=False,
            )

            with self.assertRaises(AndroidLoginError):
                asyncio.run(manager.login_if_needed())

    def test_account_login_button_requires_title_screen_colors(self):
        def pixel_at(x: int, y: int) -> tuple[int, int, int]:
            button = {(640, 512), (560, 513), (720, 513), (640, 490), (640, 536)}
            if (x, y) in button:
                return (85, 85, 85)
            return (230, 230, 230)

        self.assertTrue(account_login_button_visible(1280, 720, pixel_at))
        self.assertFalse(account_login_button_visible(1920, 1080, pixel_at))
        self.assertFalse(account_login_button_visible(1280, 1024, pixel_at))

        def scaled_pixel(x: int, y: int) -> tuple[int, int, int]:
            if 840 - 120 <= x <= 840 + 120 and 768 - 36 <= y <= 768 + 36 and abs(x - 960) <= 140:
                return (90, 90, 92)
            return (225, 228, 230)

        self.assertTrue(account_login_button_visible(1920, 1080, scaled_pixel))

        def one_noisy_sample(x: int, y: int) -> tuple[int, int, int]:
            if (x, y) == (400, 513):
                return (150, 120, 100)
            return pixel_at(x, y)

        self.assertTrue(account_login_button_visible(1280, 720, one_noisy_sample))

        def wrong_pixel(_x: int, _y: int) -> tuple[int, int, int]:
            return (20, 20, 20)

        self.assertFalse(account_login_button_visible(1280, 720, wrong_pixel))

    def test_confirms_data_expired_dialog_then_logs_in_from_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML, PASSWORD_LOGIN_UI_XML],
                title_button_visible=(1280, 720),
                data_expired_visible=(1280, 720),
            )

            logged_in = asyncio.run(manager.login_if_needed())

        self.assertTrue(logged_in)
        taps = [tuple(call[0][-2:]) for call in runner.simple_calls if "tap" in call[0]]
        self.assertEqual(taps[0], ("640", "491"))
        self.assertEqual(taps[1], ("640", "512"))
        self.assertTrue(any("input text" in script for script in manager.shell_scripts))

    def test_data_expired_dialog_without_title_button_does_not_type_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML],
                title_button_visible=None,
                data_expired_visible=(1280, 720),
            )

            with self.assertRaises(AndroidLoginError):
                asyncio.run(manager.login_if_needed())

        self.assertEqual(manager.shell_scripts, [])
        taps = [tuple(call[0][-2:]) for call in runner.simple_calls if "tap" in call[0]]
        self.assertEqual(taps, [("640", "491")])

    def test_data_expired_then_start_title_needs_no_credentials(self):
        # Daily 04:00 reset with the game left open: after confirming, the title
        # shows START (saved session). Nothing must be typed or tapped there.
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML],
                title_button_visible=None,
                data_expired_visible=(1280, 720),
                start_title=[False, False, True],
            )

            changed = asyncio.run(manager.login_if_needed())

        self.assertTrue(changed)
        self.assertEqual(manager.shell_scripts, [])
        taps = [tuple(call[0][-2:]) for call in runner.simple_calls if "tap" in call[0]]
        self.assertEqual(taps, [("640", "491")])

    def test_start_title_without_dialog_is_left_to_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML],
                title_button_visible=True,
                start_title=True,
            )

            changed = asyncio.run(manager.login_if_needed())

        self.assertFalse(changed)
        self.assertFalse([call for call in runner.simple_calls if "tap" in call[0]])

    def test_single_frame_title_button_match_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML],
            )
            results = [(1280, 720), None]

            async def flicker():
                return results.pop(0) if results else None

            manager._title_account_button_visible = flicker
            changed = asyncio.run(manager.login_if_needed())

        self.assertFalse(changed)
        self.assertFalse([call for call in runner.simple_calls if "tap" in call[0]])

    def test_title_tap_that_enters_game_without_form_is_not_a_failure(self):
        # 2026-10-21 08:11: the title tap went straight into the game (saved
        # session) and the password form never appeared.
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML],
            )
            results = [(1280, 720), (1280, 720), None]

            async def button():
                return results.pop(0) if results else None

            manager._title_account_button_visible = button
            changed = asyncio.run(manager.login_if_needed())

        self.assertTrue(changed)
        self.assertEqual(manager.shell_scripts, [])

    def test_form_missing_while_title_button_stays_saves_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            runner = FakeRunner()
            manager = TestLoginManager(
                config,
                runner.run_simple,
                ui_xmls=[EMPTY_UI_XML],
                title_button_visible=True,
            )

            with self.assertRaises(AndroidLoginError):
                asyncio.run(manager.login_if_needed())

        self.assertEqual(manager.saved_screenshots, ["login_form_missing"])

    def test_start_title_pixel_detection(self):
        def title(x: int, y: int) -> tuple[int, int, int]:
            # yellow diamond outline in the START box, black strip around it
            if 600 <= x <= 680 and 650 <= y <= 690 and (x + y) % 4 < 2:
                return (255, 216, 0)
            return (3, 3, 3)

        self.assertTrue(start_title_visible(1280, 720, title))

        def update_screen(x: int, y: int) -> tuple[int, int, int]:
            # yellow progress bar spanning the width: not the START diamond
            if 690 <= y <= 696:
                return (255, 216, 0)
            return (3, 3, 3)

        self.assertFalse(start_title_visible(1280, 720, update_screen))

        def wide_yellow(x: int, y: int) -> tuple[int, int, int]:
            return (255, 216, 0) if 650 <= y <= 690 else (3, 3, 3)

        self.assertFalse(start_title_visible(1280, 720, wide_yellow))
        self.assertFalse(start_title_visible(1280, 1024, title))

    def test_data_expired_dialog_pixel_detection(self):
        def dialog(x: int, y: int) -> tuple[int, int, int]:
            if 620 <= x <= 660 and 470 <= y <= 512:
                # white round button with a dark check mark stroke
                return (20, 20, 20) if abs((x - 640) - (y - 491)) <= 3 else (255, 255, 255)
            if 165 <= y <= 455:
                return (52, 52, 52)
            if 456 <= y <= 530:
                return (12, 12, 12)
            return (188, 188, 188)

        self.assertTrue(data_expired_dialog_visible(1280, 720, dialog))

        def scaled(x: int, y: int) -> tuple[int, int, int]:
            return dialog(round(x * 1280 / 1920), round(y * 720 / 1080))

        self.assertTrue(data_expired_dialog_visible(1920, 1080, scaled))
        self.assertFalse(data_expired_dialog_visible(1280, 1024, dialog))

        def all_dark(_x: int, _y: int) -> tuple[int, int, int]:
            return (12, 12, 12)

        self.assertFalse(data_expired_dialog_visible(1280, 720, all_dark))

        def title_button(x: int, y: int) -> tuple[int, int, int]:
            if 520 <= x <= 760 and 476 <= y <= 548:
                return (85, 85, 85)
            return (230, 230, 230)

        self.assertFalse(data_expired_dialog_visible(1280, 720, title_button))

    def test_png_pixel_at_reads_rgb_pixels(self):
        png = _rgb_png(2, 1, {(0, 0): (85, 86, 87), (1, 0): (230, 231, 232)})
        width, height, pixel_at = png_pixel_at(png)

        self.assertEqual((width, height), (2, 1))
        self.assertEqual(pixel_at(0, 0), (85, 86, 87))
        self.assertEqual(pixel_at(1, 0), (230, 231, 232))


def _rgb_png(width: int, height: int, pixels: dict[tuple[int, int], tuple[int, int, int]]) -> bytes:
    import struct
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(pixels[(x, y)])
    compressed = zlib.compress(bytes(raw))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


if __name__ == "__main__":
    unittest.main()
