from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import struct
import time
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


class LoginFormNotOpenedError(AndroidLoginError):
    """The native password form never appeared; nothing was typed."""


TITLE_STATE_START = "start"
TITLE_STATE_ACCOUNT = "account"


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


# Logged-in title screen: a yellow diamond 「START」 button at the bottom center
# on a black strip, while the background artwork rotates. Rows stop above the
# update progress bar (y=694..696) so the yellow bar never counts.
START_DIAMOND_BOX = (590, 650, 690, 690)  # left, top, right, bottom
START_FLANK_BOXES = ((420, 650, 540, 690), (740, 650, 860, 690))
START_MIN_YELLOW = 60


def _is_start_yellow(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red >= 200 and 170 <= green <= 235 and blue <= 70


def start_title_visible(width: int, height: int, pixel_at: PixelAt) -> bool:
    """Recognize the logged-in title screen (yellow 「START」 diamond, saved session).

    Tapping it enters the game without any login form, so it must never be
    mistaken for the logged-out 「账号登录」 title button.
    """
    if not _ratio_ok(width, height):
        return False

    def sample(box: tuple[int, int, int, int], step: int):
        left, top, right, bottom = box
        for ref_y in range(top, bottom + 1, step):
            for ref_x in range(left, right + 1, step):
                x, y = scale_point((ref_x, ref_y), width, height)
                if 0 <= x < width and 0 <= y < height:
                    yield pixel_at(x, y)

    yellow = sum(_is_start_yellow(pixel) for pixel in sample(START_DIAMOND_BOX, 2))
    if yellow < START_MIN_YELLOW:
        return False
    flank_yellow = 0
    flank_dark = flank_total = 0
    for box in START_FLANK_BOXES:
        for pixel in sample(box, 8):
            flank_total += 1
            flank_yellow += _is_start_yellow(pixel)
            flank_dark += max(pixel) <= 40
    return flank_yellow == 0 and flank_total > 0 and flank_dark >= 0.8 * flank_total


# Text lines (white glyphs) inside the one-button dialog band, measured on the
# 1280x720 reference layout as (top, bottom, left, right). Unity renders these
# at fixed positions, so the line geometry tells the dialogs apart without OCR.
DIALOG_TEXT_SCAN_Y = (180, 440)
DIALOG_TEXT_SCAN_X = (300, 980)
DIALOG_LINE_TOLERANCE = 10
DIALOG_CENTER_TOLERANCE = 20
DIALOG_DATA_EXPIRED = "data_expired"
DIALOG_UPDATE_FAILED = "update_failed"
DIALOG_NETWORK_LOST = "network_lost"
DIALOG_UNKNOWN = "unknown"
DIALOG_TEXT_SIGNATURES: dict[str, tuple[tuple[int, int, int, int], ...]] = {
    # 「数据文件已过期，请重新登录」
    DIALOG_DATA_EXPIRED: ((320, 343, 472, 806),),
    # 「更新下载失败，请检查网络连接后确认重试」/「错误号:207」
    DIALOG_UPDATE_FAILED: ((297, 320, 395, 885), (344, 367, 575, 702)),
    # 「与神经网络连接丢失，请重新连接」/「(错误号:0)」
    DIALOG_NETWORK_LOST: ((280, 303, 447, 833), (327, 353, 583, 696)),
}


def _ratio_ok(width: int, height: int) -> bool:
    return width > 0 and height > 0 and abs(width / height - 16 / 9) <= 0.02


def one_button_dialog_visible(width: int, height: int, pixel_at: PixelAt) -> bool:
    """Recognize the game's Unity one-button dialog frame.

    A full-width dark gray band with the message, a near-black strip below it
    and a white round confirm (check mark) button. Used by 「数据文件已过期」,
    「更新下载失败 错误号:207」 and 「与神经网络连接丢失 (错误号:0)」 alike.
    uiautomator cannot see it, so match pixels.
    """
    if not _ratio_ok(width, height):
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


def dialog_text_lines(width: int, height: int, pixel_at: PixelAt) -> list[tuple[int, int, int, int]]:
    """Return white text lines in the dialog band as reference (top, bottom, left, right)."""
    ref_width, ref_height = REFERENCE_SIZE
    x_start, x_end = DIALOG_TEXT_SCAN_X
    y_start, y_end = DIALOG_TEXT_SCAN_Y
    columns = [(ref_x, min(width - 1, round(ref_x * width / ref_width))) for ref_x in range(x_start, x_end)]
    lines: list[list[int]] = []
    current: list[int] | None = None
    gap = 0
    for ref_y in range(y_start, y_end):
        y = min(height - 1, round(ref_y * height / ref_height))
        xs = [ref_x for ref_x, x in columns if _is_text_pixel(pixel_at(x, y))]
        if len(xs) >= 2:
            if current is None:
                current = [ref_y, ref_y, xs[0], xs[-1]]
            else:
                current[1] = ref_y
                current[2] = min(current[2], xs[0])
                current[3] = max(current[3], xs[-1])
            gap = 0
        elif current is not None:
            gap += 1
            if gap > 3:
                lines.append(current)
                current = None
                gap = 0
    if current is not None:
        lines.append(current)
    return [tuple(line) for line in lines if line[1] - line[0] >= 8]  # type: ignore[misc]


def _line_matches(
    actual: tuple[int, int, int, int],
    expected: tuple[int, int, int, int],
    *,
    exact_width: bool,
) -> bool:
    tol = DIALOG_LINE_TOLERANCE
    if abs(actual[0] - expected[0]) > tol or abs(actual[1] - expected[1]) > tol:
        return False
    if exact_width:
        return abs(actual[2] - expected[2]) <= tol and abs(actual[3] - expected[3]) <= tol
    # Second lines hold the error number, which may change length; it is centered.
    actual_center = (actual[2] + actual[3]) / 2
    expected_center = (expected[2] + expected[3]) / 2
    return abs(actual_center - expected_center) <= DIALOG_CENTER_TOLERANCE


def classify_dialog_text(lines: Sequence[tuple[int, int, int, int]]) -> str:
    for kind, signature in DIALOG_TEXT_SIGNATURES.items():
        if len(lines) != len(signature):
            continue
        if all(
            _line_matches(actual, expected, exact_width=index == 0)
            for index, (actual, expected) in enumerate(zip(lines, signature))
        ):
            return kind
    return DIALOG_UNKNOWN


def classify_one_button_dialog(width: int, height: int, pixel_at: PixelAt) -> str | None:
    """Return the dialog kind (DIALOG_*) when a one-button dialog is visible, else None."""
    if not one_button_dialog_visible(width, height, pixel_at):
        return None
    return classify_dialog_text(dialog_text_lines(width, height, pixel_at))


def data_expired_dialog_visible(width: int, height: int, pixel_at: PixelAt) -> bool:
    """Recognize the Unity dialog 「数据文件已过期，请重新登录」 (one confirm button).

    Shown when the saved session expired; confirming it brings the game back to
    the title screen (sometimes after re-unpacking resources), where the
    「账号登录」 button appears. Dialogs recognized as something else (update
    download failure 207, network lost 0) are excluded: confirming those does
    not lead to the title screen and they are handled by game_recovery.
    """
    kind = classify_one_button_dialog(width, height, pixel_at)
    return kind in (DIALOG_DATA_EXPIRED, DIALOG_UNKNOWN)


async def adb_screencap_png(adb_bin: str, serial: str, timeout: float = 15) -> bytes:
    """Return a PNG screenshot from the device, or b"" on any failure."""
    try:
        process = await asyncio.create_subprocess_exec(
            adb_bin,
            "-s",
            serial,
            "exec-out",
            "screencap",
            "-p",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        return b""
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return b""
    if process.returncode != 0 or not stdout.startswith(b"\x89PNG"):
        return b""
    return stdout


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


def _is_text_pixel(pixel: tuple[int, int, int]) -> bool:
    return min(pixel) >= 200 and max(pixel) - min(pixel) < 25


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
    TITLE_CONFIRM_SECONDS = 3
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
        """Get the game past login-related screens; True means "state changed, StartUp again".

        * Native password form visible: log in.
        * 「数据文件已过期，请重新登录」 (normally the daily 04:00 server reset while
          the game stayed open): confirm it, then wait for the title screen. If the
          title shows the yellow START diamond the saved session is still valid and
          MAA StartUp can enter the game, so no credentials are needed.
        * Logged-out title with the 「账号登录」 button (seen on two captures in a
          row): tap it once and log in.

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
            state, screen_size = await self._wait_for_title_after_expired()
            if state == TITLE_STATE_START:
                logger.info("已回到标题页且显示 START（登录状态仍有效），交给 MAA StartUp 进入游戏")
                return True
            if state != TITLE_STATE_ACCOUNT:
                await self._save_debug_screenshot("login_title_timeout")
                raise AndroidLoginError(
                    "已确认「数据文件已过期」提示，但等待后既没有出现 START 也没有出现账号登录按钮"
                    "（游戏可能仍在加载或释放资源），未输入凭据"
                )
        else:
            if await self._start_title_visible():
                # Saved session; MAA StartUp taps START itself.
                return False
            screen_size = await self._confirmed_title_account_button()
            if not screen_size:
                return False

        width, height = screen_size if isinstance(screen_size, tuple) else REFERENCE_SIZE
        tap_x, tap_y = scale_point(self.ACCOUNT_LOGIN_TAP, width, height)
        logger.info("检测到官服标题页的账号登录按钮，点击 (%s,%s) 打开登录表单", tap_x, tap_y)
        # Tap the Unity title button exactly once; a second tap on the same spot
        # could land on whatever the login dialog draws there.
        await self._tap(tap_x, tap_y)
        await self._sleep(3)
        try:
            await self._login_with_timeout()
        except LoginFormNotOpenedError:
            if await self._title_account_button_visible():
                await self._save_debug_screenshot("login_form_missing")
                raise
            # The tap left the title screen without any login form: the game
            # went in with its saved session. Let StartUp take it from here.
            logger.warning("点击标题页后没有出现登录表单，游戏已离开标题页（视为登录状态有效），交给 StartUp")
            return True
        return True

    async def _confirmed_title_account_button(self) -> tuple[int, int] | bool | None:
        """The logged-out title button, seen on two captures a few seconds apart.

        A single frame is not enough: transition and loading frames can briefly
        look like the gray button on a light background.
        """
        first = await self._title_account_button_visible()
        if not first:
            return None
        await self._sleep(self.TITLE_CONFIRM_SECONDS)
        if await self._start_title_visible():
            return None
        second = await self._title_account_button_visible()
        return second or None

    async def _wait_for_title_after_expired(self) -> tuple[str | None, tuple[int, int] | bool | None]:
        account_hits = 0
        for _ in range(self.TITLE_AFTER_EXPIRED_POLLS):
            await self._sleep(self.TITLE_AFTER_EXPIRED_POLL_SECONDS)
            if await self._start_title_visible():
                return TITLE_STATE_START, None
            screen_size = await self._title_account_button_visible()
            if screen_size:
                account_hits += 1
                if account_hits >= 2:
                    return TITLE_STATE_ACCOUNT, screen_size
            else:
                account_hits = 0
        return None, None

    async def _login_with_timeout(self) -> None:
        try:
            await asyncio.wait_for(
                self._perform_official_login(),
                timeout=self.config.login.login_timeout_seconds,
            )
        except TimeoutError as exc:
            raise AndroidLoginError("明日方舟自动登录超时") from exc

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

    async def _start_title_visible(self) -> bool:
        """True when the logged-in title screen (START diamond) is visible."""
        png = await self._capture_png()
        if not png:
            return False
        try:
            width, height, pixel_at = png_pixel_at(png)
        except (ValueError, zlib.error, struct.error) as exc:
            logger.warning("解析截图失败，跳过 START 标题页识别：%s", exc)
            return False
        return start_title_visible(width, height, pixel_at)

    async def _save_debug_screenshot(self, tag: str) -> None:
        """Keep the screen of a failed login in the log dir for later diagnosis."""
        try:
            png = await self._capture_png()
            if not png:
                return
            log_dir = self.config.bot.log_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.png"
            path.write_bytes(png)
            logger.warning("已保存登录失败截图：%s", path)
        except Exception:
            logger.warning("保存登录失败截图失败", exc_info=True)

    async def _capture_png(self) -> bytes:
        return await adb_screencap_png(self.config.android.adb_bin, self.config.android.adb_serial)

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

        raise LoginFormNotOpenedError(
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
