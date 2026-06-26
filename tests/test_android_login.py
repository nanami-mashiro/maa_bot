import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from maa_tg_bot.android_login import AndroidLoginManager
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
        super().__init__(
            *args,
            sleep=self.no_sleep,
            **kwargs,
        )
        self.shell_scripts: list[str] = []
        self.ui_xmls = list(ui_xmls or [PASSWORD_LOGIN_UI_XML])

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
        self.assertTrue(any("doctor@example.com" in script for script in manager.shell_scripts))
        self.assertTrue(any("password123" in script for script in manager.shell_scripts))

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


if __name__ == "__main__":
    unittest.main()
