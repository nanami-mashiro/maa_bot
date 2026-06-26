from __future__ import annotations

import asyncio
import os
import re
import shlex
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol
from xml.etree import ElementTree

from .config import AppConfig


class CommandResult(Protocol):
    exit_code: int | None
    output: str


class RunSimple(Protocol):
    async def __call__(self, command: Sequence[str], timeout: int) -> CommandResult: ...


class AndroidLoginError(RuntimeError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


SleepFunc = Callable[[float], Awaitable[None]]


class AndroidLoginManager:
    UI_DUMP_PATH = "/sdcard/maa_tg_bot_window.xml"

    ACCOUNT_LOGIN_TAP = (640, 512)
    USERNAME_FIELD_TAP = (640, 272)
    PASSWORD_FIELD_TAP = (640, 352)
    AGREEMENT_TAP = (450, 418)
    LOGIN_BUTTON_TAP = (640, 498)
    LOGIN_TEXT_MARKERS = ("密码登录", "账号登录", "账户登录", "手机号登录", "登录账号")

    def __init__(
        self,
        config: AppConfig,
        run_simple: RunSimple,
        *,
        sleep: SleepFunc = asyncio.sleep,
    ):
        self.config = config
        self._run_simple = run_simple
        self._sleep = sleep

    async def login_if_needed(self) -> bool:
        """Log in only when the current Android UI is clearly the official login page."""
        if not self.config.login.enabled:
            return False
        root, _ = await self._dump_ui_tree()
        if root is None or not self._looks_like_official_login_screen(root):
            return False

        try:
            await asyncio.wait_for(
                self._perform_official_login(),
                timeout=self.config.login.login_timeout_seconds,
            )
        except TimeoutError as exc:
            raise AndroidLoginError("明日方舟自动登录超时") from exc
        return True

    async def _perform_official_login(self) -> None:
        await self._open_password_login_form()

        await self._tap_login_resource(
            "hg_login_view_edit_text_input_account",
            fallback=self.USERNAME_FIELD_TAP,
        )
        await self._clear_text()
        await self._input_text(self.config.login.username, label="账号")
        await self._sleep(1)

        await self._tap_login_resource(
            "hg_login_view_edit_text_input_password",
            fallback=self.PASSWORD_FIELD_TAP,
        )
        await self._clear_text()
        await self._input_text(self.config.login.password, label="密码")
        await self._sleep(1)
        await self._hide_keyboard()

        await self._tap_login_resource(
            "hg_login_view_check_box_check_agreement",
            fallback=self.AGREEMENT_TAP,
        )
        await self._sleep(1)
        await self._tap_login_resource(
            "hg_login_view_button_password_login",
            fallback=self.LOGIN_BUTTON_TAP,
        )
        await self._sleep(25)

    async def _open_password_login_form(self) -> None:
        last_ui = ""
        for _ in range(6):
            root, ui_xml = await self._dump_ui_tree()
            last_ui = ui_xml or last_ui
            if root is not None and self._password_form_visible(root):
                return

            if root is not None and await self._tap_ui_text(
                root,
                "密码登录",
                fallback=None,
            ):
                await self._sleep(2)
                continue

            await self._tap(*self.ACCOUNT_LOGIN_TAP)
            await self._sleep(3)

        root, ui_xml = await self._dump_ui_tree()
        last_ui = ui_xml or last_ui
        if root is not None and self._password_form_visible(root):
            return

        raise AndroidLoginError(
            "未能打开明日方舟密码登录表单",
            output=last_ui[-4000:],
        )

    async def _tap_login_resource(
        self,
        resource_suffix: str,
        *,
        fallback: tuple[int, int],
    ) -> None:
        root, _ = await self._dump_ui_tree()
        if root is not None:
            node = self._find_login_resource(root, resource_suffix)
            center = self._node_center(node) if node is not None else None
            if center is not None:
                await self._tap(*center)
                return
        await self._tap(*fallback)

    async def _tap_ui_text(
        self,
        root: ElementTree.Element,
        text: str,
        *,
        fallback: tuple[int, int] | None,
    ) -> bool:
        node = self._find_ui_text(root, text)
        center = self._node_center(node) if node is not None else None
        if center is not None:
            await self._tap(*center)
            return True
        if fallback is None:
            return False
        await self._tap(*fallback)
        return True

    async def _dump_ui_tree(self) -> tuple[ElementTree.Element | None, str]:
        ui_xml = await self._dump_ui_xml()
        if not ui_xml:
            return None, ""
        try:
            return ElementTree.fromstring(ui_xml), ui_xml
        except ElementTree.ParseError:
            return None, ui_xml

    async def _dump_ui_xml(self) -> str:
        dump = await self._run_simple(
            [
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "shell",
                "uiautomator",
                "dump",
                self.UI_DUMP_PATH,
            ],
            timeout=10,
        )
        if dump.exit_code != 0:
            return dump.output

        read = await self._run_simple(
            [
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "shell",
                "cat",
                self.UI_DUMP_PATH,
            ],
            timeout=10,
        )
        if read.exit_code != 0:
            return read.output
        return read.output

    def _has_login_resource(self, root: ElementTree.Element, resource_suffix: str) -> bool:
        return self._find_login_resource(root, resource_suffix) is not None

    def _password_form_visible(self, root: ElementTree.Element) -> bool:
        return self._has_login_resource(
            root,
            "hg_login_view_edit_text_input_account",
        ) and self._has_login_resource(root, "hg_login_view_edit_text_input_password")

    def _looks_like_official_login_screen(self, root: ElementTree.Element) -> bool:
        if self._password_form_visible(root):
            return True
        for node in root.iter("node"):
            resource_id = node.attrib.get("resource-id", "")
            if ":id/hg_login_view_" in resource_id or resource_id.startswith("hg_login_view_"):
                return True
        return any(self._find_ui_text(root, marker) is not None for marker in self.LOGIN_TEXT_MARKERS)

    def _find_login_resource(
        self,
        root: ElementTree.Element,
        resource_suffix: str,
    ) -> ElementTree.Element | None:
        suffix = f":id/{resource_suffix}"
        for node in root.iter("node"):
            resource_id = node.attrib.get("resource-id", "")
            if resource_id == resource_suffix or resource_id.endswith(suffix):
                return node
        return None

    @staticmethod
    def _find_ui_text(root: ElementTree.Element, text: str) -> ElementTree.Element | None:
        for node in root.iter("node"):
            if text in node.attrib.get("text", ""):
                return node
        return None

    @staticmethod
    def _node_center(node: ElementTree.Element | None) -> tuple[int, int] | None:
        if node is None:
            return None
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
        if not match:
            return None
        left, top, right, bottom = (int(value) for value in match.groups())
        return ((left + right) // 2, (top + bottom) // 2)

    async def _tap(self, x: int, y: int) -> None:
        result = await self._run_simple(
            [
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "shell",
                "input",
                "tap",
                str(x),
                str(y),
            ],
            timeout=10,
        )
        if result.exit_code != 0:
            raise AndroidLoginError(f"ADB 点击失败：{x},{y}", output=result.output)

    async def _hide_keyboard(self) -> None:
        await self._keyevent("KEYCODE_BACK", error_message="ADB 隐藏键盘失败")
        await self._sleep(1)

    async def _keyevent(
        self,
        key: str,
        *,
        error_message: str = "ADB 按键失败",
    ) -> None:
        result = await self._run_simple(
            [
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "shell",
                "input",
                "keyevent",
                key,
            ],
            timeout=10,
        )
        if result.exit_code != 0:
            raise AndroidLoginError(error_message, output=result.output)

    async def _clear_text(self) -> None:
        script = (
            "input keyevent KEYCODE_MOVE_END\n"
            "i=0\n"
            "while [ $i -lt 80 ]; do input keyevent KEYCODE_DEL; i=$((i+1)); done\n"
        )
        await self._run_adb_shell_script(script, timeout=20, error_message="ADB 清空文本失败")

    async def _input_text(self, value: str, *, label: str) -> None:
        text_arg = self._adb_input_text_arg(value, label=label)
        await self._run_adb_shell_script(
            f"input text {text_arg}\n",
            timeout=20,
            error_message=f"ADB 输入{label}失败",
        )

    async def _run_adb_shell_script(
        self,
        script: str,
        *,
        timeout: int,
        error_message: str,
    ) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "shell",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(script.encode("utf-8")),
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise AndroidLoginError(error_message, output=str(exc)) from exc
        except TimeoutError as exc:
            raise AndroidLoginError(error_message, output="ADB shell 命令超时") from exc

        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        if process.returncode != 0:
            raise AndroidLoginError(error_message, output=output)

    @staticmethod
    def _adb_input_text_arg(value: str, *, label: str) -> str:
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise AndroidLoginError(
                f"当前 ADB 输入模式下，自动登录只支持 ASCII {label}"
            ) from exc
        escaped = value.replace("%", "%25").replace(" ", "%s")
        return shlex.quote(escaped)
