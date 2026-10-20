import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from maa_tg_bot.android_login import (
    AndroidLoginManager,
    account_login_button_visible,
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
        super().__init__(
            *args,
            sleep=self.no_sleep,
            **kwargs,
        )
        self.shell_scripts: list[str] = []
        self.ui_xmls = list(ui_xmls or [PASSWORD_LOGIN_UI_XML])

    async def _title_account_button_visible(self) -> bool:
        return self.title_button_visible

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

    def test_opens_password_login_form_from_title(self):
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
            self.assertEqual(tap_calls[0][0][-2:], ["640", "512"])
            self.assertEqual(tap_calls[1][0][-2:], ["812", "562"])

    def test_accepts_password_form_after_last_account_login_tap(self):
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
            self.assertEqual(len(tap_calls), 6)
            self.assertEqual(tap_calls[-1][0][-2:], ["640", "512"])

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
        self.assertTrue(any("doctor@example.com" in script for script in manager.shell_scripts))

    def test_account_login_button_requires_title_screen_colors(self):
        def pixel_at(x: int, y: int) -> tuple[int, int, int]:
            button = {(640, 512), (560, 513), (720, 513), (640, 490), (640, 536)}
            if (x, y) in button:
                return (85, 85, 85)
            return (230, 230, 230)

        self.assertTrue(account_login_button_visible(1280, 720, pixel_at))
        self.assertFalse(account_login_button_visible(1920, 1080, pixel_at))

        def wrong_pixel(_x: int, _y: int) -> tuple[int, int, int]:
            return (20, 20, 20)

        self.assertFalse(account_login_button_visible(1280, 720, wrong_pixel))

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
