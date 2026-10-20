from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import struct
import zlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol
from xml.etree import ElementTree

from .config import AppConfig

logger = logging.getLogger(__name__)


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
PixelAt = Callable[[int, int], tuple[int, int, int]]


REFERENCE_SIZE = (1280, 720)
TITLE_BUTTON_POINTS = ((640, 512), (560, 513), (720, 513), (640, 490), (640, 536))
TITLE_BACKGROUND_POINTS = ((400, 513), (900, 513), (640, 460), (640, 560))
# 「数据文件已过期，请重新登录」: a full-width dark gray band with the message,
# a near-black strip below it and a white round confirm (check mark) button.
# Only the right half is sampled; the left half shows a textured background.
DATA_EXPIRED_BAND_POINTS = (
    (780, 220), (980, 220), (1180, 220),
    (780, 300), (980, 300), (1180, 300),
    (780, 380), (980, 380), (1180, 380),
)
DATA_EXPIRED_STRIP_POINTS = ((900, 475), (1000, 475), (1180, 475), (900, 510), (1000, 510), (1180, 510))
DATA_EXPIRED_CONFIRM_TAP = (640, 491)
DATA_EXPIRED_CONFIRM_BOX = (615, 466, 665, 516)


def scale_point(point: tuple[int, int], width: int, height: int) -> tuple[int, int]:
    """Map a coordinate measured on the 1280x720 reference layout to the actual screen."""
    ref_width, ref_height = REFERENCE_SIZE
    return (round(point[0] * width / ref_width), round(point[1] * height / ref_height))


def account_login_button_visible(width: int, height: int, pixel_at: PixelAt) -> bool:
    """Recognize the logged-out official title button (「账号登录」).

    The title screen is drawn by Unity, so uiautomator cannot see the button.
    It is a dark gray rectangle centered near (640, 512) on the 1280x720 reference
    layout, surrounded by the light title background. Coordinates are scaled to the
    real screenshot size (16:9 only) and one sample of each group may miss, so a
    slightly shifted render or a bit of anti-aliasing does not break detection.
    """
    if width <= 0 or height <= 0 or abs(width / height - 16 / 9) > 0.02:
        return False

    def matches(points: tuple[tuple[int, int], ...], predicate: Callable[[tuple[int, int, int]], bool]) -> int:
        count = 0
        for point in points:
            x, y = scale_point(point, width, height)
            if 0 <= x < width and 0 <= y < height and predicate(pixel_at(x, y)):
                count += 1
        return count

    button_hits = matches(TITLE_BUTTON_POINTS, _is_login_button_pixel)
    background_hits = matches(TITLE_BACKGROUND_POINTS, _is_login_background_pixel)
    return (
        _is_login_button_pixel(pixel_at(*scale_point(TITLE_BUTTON_POINTS[0], width, height)))
        and button_hits >= len(TITLE_BUTTON_POINTS) - 1
        and background_hits >= len(TITLE_BACKGROUND_POINTS) - 1
    )


def data_expired_dialog_visible(width: int, height: int, pixel_at: PixelAt) -> bool:
    """Recognize the Unity dialog 「数据文件已过期，请重新登录」 (one confirm button).

    Shown when the saved session expired; confirming it brings the game back to
    the title screen (sometimes after re-unpacking resources), where the
    「账号登录」 button appears. uiautomator cannot see it, so match pixels.
    """
    if width <= 0 or height <= 0 or abs(width / height - 16 / 9) > 0.02:
        return False

    def hits(points: tuple[tuple[int, int], ...], predicate: Callable[[tuple[int, int, int]], bool]) -> int:
        count = 0
        for point in points:
            x, y = scale_point(point, width, height)
            if 0 <= x < width and 0 <= y < height and predicate(pixel_at(x, y)):
                count += 1
        return count

    if hits(DATA_EXPIRED_BAND_POINTS, _is_dialog_band_pixel) < len(DATA_EXPIRED_BAND_POINTS) - 1:
        return False
    if hits(DATA_EXPIRED_STRIP_POINTS, _is_dialog_strip_pixel) < len(DATA_EXPIRED_STRIP_POINTS) - 1:
        return False
    left, top, right, bottom = DATA_EXPIRED_CONFIRM_BOX
    samples = white = 0
    for ref_y in range(top, bottom + 1, 4):
        for ref_x in range(left, right + 1, 4):
            x, y = scale_point((ref_x, ref_y), width, height)
            if not (0 <= x < width and 0 <= y < height):
                continue
            samples += 1
            red, green, blue = pixel_at(x, y)
            if red >= 225 and green >= 225 and blue >= 225:
                white += 1
    # The round button is mostly white with a dark check mark; a solid white or
    # solid dark area is something else.
    return samples > 0 and 0.2 <= white / samples <= 0.9


def png_pixel_at(data: bytes) -> tuple[int, int, PixelAt]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("不是 PNG 截图")
    width = height = None
    color_type = None
    bit_depth = None
    idat: list[bytes] = []
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif chunk_type == b"IDAT":
            idat.append(chunk)
        elif chunk_type == b"IEND":
            break
    if width is None or height is None or color_type not in (2, 6) or bit_depth != 8:
        raise ValueError("不支持的 PNG 截图格式")
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    raw = zlib.decompress(b"".join(idat))
    rows: list[bytearray] = []
    cursor = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        _unfilter_png_row(row, previous, filter_type, channels)
        previous = row
        rows.append(row)

    def pixel_at(x: int, y: int) -> tuple[int, int, int]:
        offset = x * channels
        pixel = rows[y][offset : offset + 3]
        return pixel[0], pixel[1], pixel[2]

    return width, height, pixel_at


def _is_login_button_pixel(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return 60 <= red <= 120 and abs(red - green) < 16 and abs(green - blue) < 16


def _is_dialog_band_pixel(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return 40 <= red <= 70 and abs(red - green) < 8 and abs(green - blue) < 8


def _is_dialog_strip_pixel(pixel: tuple[int, int, int]) -> bool:
    return max(pixel) <= 32


def _is_login_background_pixel(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red >= 190 and green >= 190 and blue >= 190 and abs(red - green) < 20 and abs(green - blue) < 20


def _unfilter_png_row(row: bytearray, previous: bytearray, filter_type: int, channels: int) -> None:
    if filter_type == 0:
        return
    for index in range(len(row)):
        left = row[index - channels] if index >= channels else 0
        up = previous[index]
        up_left = previous[index - channels] if index >= channels else 0
        if filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = up
        elif filter_type == 3:
            predictor = (left + up) // 2
        elif filter_type == 4:
            predictor = _paeth(left, up, up_left)
        else:
            raise ValueError(f"不支持的 PNG 过滤器：{filter_type}")
        row[index] = (row[index] + predictor) & 255


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    up_left_distance = abs(estimate - up_left)
    if left_distance <= up_distance and left_distance <= up_left_distance:
        return left
    if up_distance <= up_left_distance:
        return up
    return up_left


class AndroidLoginManager:
    UI_DUMP_PATH = "/sdcard/maa_tg_bot_window.xml"

    ACCOUNT_LOGIN_TAP = (640, 512)
    USERNAME_FIELD_TAP = (640, 272)
    PASSWORD_FIELD_TAP = (640, 352)
    AGREEMENT_TAP = (450, 418)
    LOGIN_BUTTON_TAP = (640, 498)
    LOGIN_TEXT_MARKERS = ("密码登录", "账号登录", "账户登录", "手机号登录", "登录账号")
    FORM_OPEN_POLLS = 10
    TITLE_AFTER_EXPIRED_POLLS = 36
    TITLE_AFTER_EXPIRED_POLL_SECONDS = 5
    LOGIN_RESULT_POLLS = 8
    LOGIN_RESULT_POLL_SECONDS = 5

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
        """Log in when the native login form or the logged-out title button is visible.

        Credentials are only typed after uiautomator confirms the native password
        form (account + password inputs). If the form never shows up, this raises
        AndroidLoginError without typing anything.
        """
        if not self.config.login.enabled:
            return False
        root, _ = await self._dump_ui_tree()
        if root is not None and self._looks_like_official_login_screen(root):
            await self._login_with_timeout()
            return True

        expired_size = await self._data_expired_dialog_visible()
        if expired_size:
            width, height = expired_size
            tap_x, tap_y = scale_point(DATA_EXPIRED_CONFIRM_TAP, width, height)
            logger.info("检测到「数据文件已过期，请重新登录」提示，点击确认 (%s,%s)", tap_x, tap_y)
            await self._tap(tap_x, tap_y)
            screen_size = await self._wait_for_title_account_button()
            if not screen_size:
                raise AndroidLoginError(
                    "已确认「数据文件已过期」提示，但等待后仍未出现标题页账号登录按钮"
                    "（游戏可能仍在释放资源），未输入凭据"
                )
        else:
            screen_size = await self._title_account_button_visible()
            if not screen_size:
                return False

        width, height = screen_size if isinstance(screen_size, tuple) else REFERENCE_SIZE
        tap_x, tap_y = scale_point(self.ACCOUNT_LOGIN_TAP, width, height)
        logger.info("检测到官服标题页的账号登录按钮，点击 (%s,%s) 打开登录表单", tap_x, tap_y)
        # Tap the Unity title button exactly once; a second tap on the same spot
        # could land on whatever the login dialog draws there.
        await self._tap(tap_x, tap_y)
        await self._sleep(3)
        await self._login_with_timeout()
        return True

    async def _login_with_timeout(self) -> None:
        try:
            await asyncio.wait_for(
                self._perform_official_login(),
                timeout=self.config.login.login_timeout_seconds,
            )
        except TimeoutError as exc:
            raise AndroidLoginError("明日方舟自动登录超时") from exc

    async def _wait_for_title_account_button(self) -> tuple[int, int] | bool | None:
        for _ in range(self.TITLE_AFTER_EXPIRED_POLLS):
            await self._sleep(self.TITLE_AFTER_EXPIRED_POLL_SECONDS)
            screen_size = await self._title_account_button_visible()
            if screen_size:
                return screen_size
        return None

    async def _data_expired_dialog_visible(self) -> tuple[int, int] | None:
        """Return the screen size when the 「数据文件已过期」 dialog is visible, else None."""
        png = await self._capture_png()
        if not png:
            return None
        try:
            width, height, pixel_at = png_pixel_at(png)
        except (ValueError, zlib.error, struct.error) as exc:
            logger.warning("解析截图失败，跳过数据过期提示识别：%s", exc)
            return None
        if data_expired_dialog_visible(width, height, pixel_at):
            return (width, height)
        return None

    async def _title_account_button_visible(self) -> tuple[int, int] | None:
        """Return the screen size when the title account button is visible, else None."""
        png = await self._capture_png()
        if not png:
            return None
        try:
            width, height, pixel_at = png_pixel_at(png)
        except (ValueError, zlib.error, struct.error) as exc:
            logger.warning("解析截图失败，跳过标题页登录识别：%s", exc)
            return None
        if account_login_button_visible(width, height, pixel_at):
            return (width, height)
        return None

    async def _capture_png(self) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                self.config.android.adb_bin,
                "-s",
                self.config.android.adb_serial,
                "exec-out",
                "screencap",
                "-p",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        except (FileNotFoundError, TimeoutError):
            return b""
        if process.returncode != 0 or not stdout.startswith(b"\x89PNG"):
            return b""
        return stdout

    async def _perform_official_login(self) -> None:
        await self._open_password_login_form()

        await self._require_password_form("输入账号前")
        await self._tap_login_resource(
            "hg_login_view_edit_text_input_account",
            fallback=self.USERNAME_FIELD_TAP,
        )
        await self._clear_text()
        await self._input_text(self.config.login.username, label="账号")
        await self._sleep(1)
        await self._hide_keyboard()

        # KEYCODE_BACK may close the dialog if no keyboard was open, so check
        # again before the password goes anywhere.
        await self._require_password_form("输入密码前")
        await self._tap_login_resource(
            "hg_login_view_edit_text_input_password",
            fallback=self.PASSWORD_FIELD_TAP,
        )
        await self._clear_text()
        await self._input_text(self.config.login.password, label="密码")
        await self._sleep(1)
        await self._hide_keyboard()

        await self._require_password_form("点击登录前")
        await self._ensure_agreement_checked()
        await self._sleep(1)
        await self._tap_login_resource(
            "hg_login_view_button_password_login",
            fallback=self.LOGIN_BUTTON_TAP,
        )
        await self._wait_for_login_form_closed()

    async def _require_password_form(self, stage: str) -> None:
        root, ui_xml = await self._dump_ui_tree()
        if root is None or not self._password_form_visible(root):
            raise AndroidLoginError(
                f"{stage}未确认到密码登录表单，已停止自动登录（未输入凭据）",
                output=ui_xml[-4000:],
            )

    async def _wait_for_login_form_closed(self) -> None:
        last_ui = ""
        for _ in range(self.LOGIN_RESULT_POLLS):
            await self._sleep(self.LOGIN_RESULT_POLL_SECONDS)
            root, ui_xml = await self._dump_ui_tree()
            last_ui = ui_xml or last_ui
            if root is None or not self._password_form_visible(root):
                logger.info("明日方舟登录表单已关闭，视为登录提交成功")
                return
        raise AndroidLoginError(
            "点击登录后登录表单仍未关闭，可能账号密码错误或需要验证",
            output=last_ui[-4000:],
        )

    async def _open_password_login_form(self) -> None:
        """Wait for the native password form, switching from phone login if needed.

        Never taps the Unity title button here: login_if_needed already did that
        once, and the native dialog may be drawn on top of the same coordinates.
        """
        last_ui = ""
        for _ in range(self.FORM_OPEN_POLLS):
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

            await self._sleep(2)

        root, ui_xml = await self._dump_ui_tree()
        last_ui = ui_xml or last_ui
        if root is not None and self._password_form_visible(root):
            return

        raise AndroidLoginError(
            "未能打开明日方舟密码登录表单（未输入凭据）",
            output=last_ui[-4000:],
        )

    async def _ensure_agreement_checked(self) -> None:
        root, _ = await self._dump_ui_tree()
        node = (
            self._find_login_resource(root, "hg_login_view_check_box_check_agreement")
            if root is not None
            else None
        )
        if node is not None and node.attrib.get("checked") == "true":
            return
        await self._tap_login_resource(
            "hg_login_view_check_box_check_agreement",
            fallback=self.AGREEMENT_TAP,
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
        # One input process. A shell loop of separate keyevents times out on redroid.
        deletes = " ".join(["KEYCODE_DEL"] * 40)
        await self._run_adb_shell_script(
            f"input keyevent KEYCODE_MOVE_END {deletes}\n",
            timeout=20,
            error_message="ADB 清空文本失败",
        )

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
