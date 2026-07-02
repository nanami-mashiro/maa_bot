from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ConfigError(f"Invalid boolean value: {value!r}")


def _as_int(value: Any, name: str, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer for {name}: {value!r}") from exc


def _as_float(value: Any, name: str, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid float for {name}: {value!r}") from exc


def _as_str_list(value: Any, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        if not value.strip():
            return []
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    raise ConfigError(f"Invalid list value: {value!r}")


def _as_int_list(value: Any, name: str, default: list[int] | None = None) -> list[int]:
    items = _as_str_list(
        value,
        [str(item) for item in default] if default is not None else None,
    )
    result: list[int] = []
    for item in items:
        try:
            result.append(int(item))
        except ValueError as exc:
            raise ConfigError(f"Invalid integer for {name}: {item!r}") from exc
    return result


def _as_int_set(value: Any) -> set[int]:
    items = _as_str_list(value)
    result: set[int] = set()
    for item in items:
        try:
            result.add(int(item))
        except ValueError as exc:
            raise ConfigError(f"Invalid Telegram user id: {item!r}") from exc
    return result


def _as_dict_list(value: Any, name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of tables")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"{name}[{index}] must be a table")
        result.append(dict(item))
    return result


def _as_time_list(value: Any, name: str, default: list[str]) -> list[str]:
    result = _as_str_list(value, default)
    if not result:
        raise ConfigError(f"{name} must contain at least one HH:MM time")
    normalized: list[str] = []
    for item in result:
        parts = item.split(":")
        if len(parts) != 2:
            raise ConfigError(f"Invalid time for {name}: {item!r}")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError as exc:
            raise ConfigError(f"Invalid time for {name}: {item!r}") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ConfigError(f"Invalid time for {name}: {item!r}")
        normalized.append(f"{hour:02d}:{minute:02d}")
    return normalized


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"Section [{name}] must be a table")
    return value


def _env_first(env: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value is not None and value != "":
            return value
    return None


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    allowed_user_ids: set[int]
    proxy: str | None = None


@dataclass(frozen=True)
class BotConfig:
    log_dir: Path = Path("/data/logs")
    queue_size: int = 20


@dataclass(frozen=True)
class MaaConfig:
    bin: str = "maa"
    config_dir: Path = Path("/data/maa-config")
    profile: str = "default"
    task_timeout_seconds: int = 7200
    client_type: str = "Official"
    account_name: str = ""
    server: str = "CN"
    auto_startup: bool = True
    auto_closedown: bool = False
    startup_retries: int = 3
    log_level: str = "info"


@dataclass(frozen=True)
class ClientUpdateConfig:
    enabled: bool = True
    download_url: str = "https://ak.hypergryph.com/downloads/android_lastest"
    download_timeout_seconds: int = 1800
    install_timeout_seconds: int = 600


@dataclass(frozen=True)
class MaaCoreUpdateConfig:
    enabled: bool = False
    channel: str = "stable"
    interval_seconds: int = 21600
    timeout_seconds: int = 1800
    test_time: int = 0


@dataclass(frozen=True)
class AndroidConfig:
    adb_bin: str = "adb"
    adb_serial: str = "redroid:5555"
    adb_connect_host: str = "redroid:5555"
    screenshot_timeout_seconds: int = 30


@dataclass(frozen=True)
class LoginConfig:
    enabled: bool = False
    package_name: str = "com.hypergryph.arknights"
    login_timeout_seconds: int = 180
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class FightConfig:
    stage: str = ""
    medicine: int = 0
    expiring_medicine: int = 0
    stone: int = 0
    times: int | None = None
    series: int | None = 0
    dr_grandet: bool = False


@dataclass(frozen=True)
class BaseConfig:
    mode: int = 0
    facility: list[str] = field(
        default_factory=lambda: ["Mfg", "Trade", "Reception", "Control", "Power", "Office", "Dorm"]
    )
    filename: str = ""
    plan_index: int | None = None
    drones: str = "_NotUse"
    threshold: float = 0.3
    replenish: bool = False
    dorm_notstationed_enabled: bool = False
    dorm_trust_enabled: bool = False
    reception_message_board: bool = True
    reception_clue_exchange: bool = True
    reception_send_clue: bool = True
    variants: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MallConfig:
    visit_friends: bool = True
    shopping: bool = True
    buy_first: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    force_shopping_if_credit_full: bool = False
    only_buy_discount: bool = False
    reserve_max_credit: bool = False
    credit_fight: bool = False
    formation_index: int = 0


@dataclass(frozen=True)
class AwardConfig:
    award: bool = True
    mail: bool = True
    recruit: bool = False
    orundum: bool = False
    mining: bool = False
    specialaccess: bool = False


@dataclass(frozen=True)
class RecruitConfig:
    enabled: bool = True
    refresh: bool = True
    select: list[int] = field(default_factory=lambda: [5, 4])
    confirm: list[int] = field(default_factory=lambda: [5, 4])
    times: int = 4
    set_time: bool = True
    expedite: bool = True
    expedite_times: int | None = None
    skip_robot: bool = True
    extra_tags_mode: int = 0


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool = False
    daily_times: list[str] = field(default_factory=lambda: ["08:00", "20:00"])
    timezone: str = "Asia/Shanghai"
    notify_chat_id: int | None = None


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    bot: BotConfig = field(default_factory=BotConfig)
    maa: MaaConfig = field(default_factory=MaaConfig)
    client_update: ClientUpdateConfig = field(default_factory=ClientUpdateConfig)
    maa_update: MaaCoreUpdateConfig = field(default_factory=MaaCoreUpdateConfig)
    android: AndroidConfig = field(default_factory=AndroidConfig)
    login: LoginConfig = field(default_factory=LoginConfig)
    fight: FightConfig = field(default_factory=FightConfig)
    base: BaseConfig = field(default_factory=BaseConfig)
    mall: MallConfig = field(default_factory=MallConfig)
    award: AwardConfig = field(default_factory=AwardConfig)
    recruit: RecruitConfig = field(default_factory=RecruitConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)


def default_config_paths() -> list[Path]:
    return [
        Path(os.environ["MAA_TG_CONFIG"]),
    ] if os.environ.get("MAA_TG_CONFIG") else [
        Path("/app/config/bot.toml"),
        Path("config/bot.toml"),
    ]


def load_toml_config(path: Path | None = None) -> dict[str, Any]:
    paths = [path] if path else default_config_paths()
    for candidate in paths:
        if candidate.exists():
            with candidate.open("rb") as file:
                return tomllib.load(file)
    return {}


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> AppConfig:
    env = env if env is not None else os.environ
    raw = load_toml_config(path)

    telegram = _section(raw, "telegram")
    bot = _section(raw, "bot")
    maa = _section(raw, "maa")
    client_update = _section(raw, "client_update")
    maa_update = _section(raw, "maa_update")
    android = _section(raw, "android")
    login = _section(raw, "login")
    fight = _section(raw, "fight")
    base = _section(raw, "base")
    mall = _section(raw, "mall")
    award = _section(raw, "award")
    recruit = _section(raw, "recruit")
    schedule = _section(raw, "schedule")

    token = _env_first(env, "TELEGRAM_BOT_TOKEN", "BOT_TOKEN") or str(telegram.get("token", ""))
    allowed_ids_value = _env_first(env, "TELEGRAM_ALLOWED_USER_IDS", "ALLOWED_USER_IDS")
    allowed_ids = _as_int_set(
        allowed_ids_value if allowed_ids_value is not None else telegram.get("allowed_user_ids", [])
    )

    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is required")
    if not allowed_ids:
        raise ConfigError("At least one Telegram allowed user id is required")

    # python-telegram-bot/httpx do not honour the HTTP(S)_PROXY env vars
    # automatically, so resolve a proxy explicitly and pass it to the builder.
    proxy = (
        _env_first(
            env,
            "TELEGRAM_PROXY",
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
        or (str(telegram.get("proxy", "")).strip() or None)
    )

    login_enabled = _as_bool(
        _env_first(env, "ARKNIGHTS_LOGIN_ENABLED"),
        _as_bool(login.get("enabled"), False),
    )
    login_username = _env_first(env, "ARKNIGHTS_LOGIN_USERNAME") or ""
    login_password = _env_first(env, "ARKNIGHTS_LOGIN_PASSWORD") or ""
    if login_enabled and (not login_username or not login_password):
        raise ConfigError(
            "ARKNIGHTS_LOGIN_USERNAME and ARKNIGHTS_LOGIN_PASSWORD are required "
            "when [login].enabled is true"
        )

    config = AppConfig(
        telegram=TelegramConfig(token=token, allowed_user_ids=allowed_ids, proxy=proxy),
        bot=BotConfig(
            log_dir=Path(_env_first(env, "BOT_LOG_DIR") or bot.get("log_dir", "/data/logs")),
            queue_size=int(_env_first(env, "BOT_QUEUE_SIZE") or bot.get("queue_size", 20)),
        ),
        maa=MaaConfig(
            bin=_env_first(env, "MAA_BIN") or str(maa.get("bin", "maa")),
            config_dir=Path(
                _env_first(env, "MAA_CONFIG_DIR") or maa.get("config_dir", "/data/maa-config")
            ),
            profile=_env_first(env, "MAA_PROFILE") or str(maa.get("profile", "default")),
            task_timeout_seconds=int(
                _env_first(env, "MAA_TASK_TIMEOUT_SECONDS")
                or maa.get("task_timeout_seconds", 7200)
            ),
            client_type=_env_first(env, "MAA_CLIENT_TYPE") or str(maa.get("client_type", "Official")),
            account_name=_env_first(env, "MAA_ACCOUNT_NAME") or str(maa.get("account_name", "")),
            server=_env_first(env, "MAA_SERVER") or str(maa.get("server", "CN")),
            auto_startup=_as_bool(
                _env_first(env, "MAA_AUTO_STARTUP"), _as_bool(maa.get("auto_startup"), True)
            ),
            auto_closedown=_as_bool(
                _env_first(env, "MAA_AUTO_CLOSEDOWN"), _as_bool(maa.get("auto_closedown"), False)
            ),
            startup_retries=int(
                _env_first(env, "MAA_STARTUP_RETRIES") or maa.get("startup_retries", 3)
            ),
            log_level=_env_first(env, "MAA_LOG_LEVEL") or str(maa.get("log_level", "info")),
        ),
        client_update=ClientUpdateConfig(
            enabled=_as_bool(
                _env_first(env, "CLIENT_UPDATE_ENABLED"),
                _as_bool(client_update.get("enabled"), True),
            ),
            download_url=_env_first(env, "CLIENT_UPDATE_DOWNLOAD_URL")
            or str(
                client_update.get(
                    "download_url",
                    "https://ak.hypergryph.com/downloads/android_lastest",
                )
            ),
            download_timeout_seconds=int(
                _env_first(env, "CLIENT_UPDATE_DOWNLOAD_TIMEOUT_SECONDS")
                or client_update.get("download_timeout_seconds", 1800)
            ),
            install_timeout_seconds=int(
                _env_first(env, "CLIENT_UPDATE_INSTALL_TIMEOUT_SECONDS")
                or client_update.get("install_timeout_seconds", 600)
            ),
        ),
        maa_update=MaaCoreUpdateConfig(
            enabled=_as_bool(
                _env_first(env, "MAA_CORE_UPDATE_ENABLED"),
                _as_bool(maa_update.get("enabled"), False),
            ),
            channel=_env_first(env, "MAA_CORE_UPDATE_CHANNEL")
            or str(maa_update.get("channel", "stable")),
            interval_seconds=int(
                _env_first(env, "MAA_CORE_UPDATE_INTERVAL_SECONDS")
                or maa_update.get("interval_seconds", 21600)
            ),
            timeout_seconds=int(
                _env_first(env, "MAA_CORE_UPDATE_TIMEOUT_SECONDS")
                or maa_update.get("timeout_seconds", 1800)
            ),
            test_time=int(
                _env_first(env, "MAA_CORE_UPDATE_TEST_TIME") or maa_update.get("test_time", 0)
            ),
        ),
        android=AndroidConfig(
            adb_bin=_env_first(env, "ADB_BIN") or str(android.get("adb_bin", "adb")),
            adb_serial=_env_first(env, "ADB_SERIAL", "MAA_ADB_SERIAL")
            or str(android.get("adb_serial", "redroid:5555")),
            adb_connect_host=_env_first(env, "ADB_CONNECT_HOST")
            or str(android.get("adb_connect_host", "redroid:5555")),
            screenshot_timeout_seconds=int(
                _env_first(env, "SCREENSHOT_TIMEOUT_SECONDS")
                or android.get("screenshot_timeout_seconds", 30)
            ),
        ),
        login=LoginConfig(
            enabled=login_enabled,
            package_name=_env_first(env, "ARKNIGHTS_LOGIN_PACKAGE_NAME")
            or str(login.get("package_name", "com.hypergryph.arknights")),
            login_timeout_seconds=int(
                _env_first(env, "ARKNIGHTS_LOGIN_TIMEOUT_SECONDS")
                or login.get("login_timeout_seconds", 180)
            ),
            username=login_username,
            password=login_password,
        ),
        fight=FightConfig(
            stage=_env_first(env, "MAA_DEFAULT_STAGE") or str(fight.get("stage", "")),
            medicine=int(_env_first(env, "MAA_MEDICINE") or fight.get("medicine", 0)),
            expiring_medicine=int(
                _env_first(env, "MAA_EXPIRING_MEDICINE") or fight.get("expiring_medicine", 0)
            ),
            stone=int(_env_first(env, "MAA_STONE") or fight.get("stone", 0)),
            times=_as_int(_env_first(env, "MAA_TIMES") or fight.get("times"), "fight.times"),
            series=_as_int(_env_first(env, "MAA_SERIES") or fight.get("series", 0), "fight.series"),
            dr_grandet=_as_bool(
                _env_first(env, "MAA_DR_GRANDET"), _as_bool(fight.get("dr_grandet"), False)
            ),
        ),
        base=BaseConfig(
            mode=int(base.get("mode", 0)),
            facility=_as_str_list(
                base.get("facility"),
                ["Mfg", "Trade", "Reception", "Control", "Power", "Office", "Dorm"],
            ),
            filename=str(base.get("filename", "")).strip(),
            plan_index=_as_int(base.get("plan_index"), "base.plan_index"),
            drones=str(base.get("drones", "_NotUse")),
            threshold=_as_float(base.get("threshold"), "base.threshold", 0.3),
            replenish=_as_bool(base.get("replenish"), False),
            dorm_notstationed_enabled=_as_bool(base.get("dorm_notstationed_enabled"), False),
            dorm_trust_enabled=_as_bool(base.get("dorm_trust_enabled"), False),
            reception_message_board=_as_bool(base.get("reception_message_board"), True),
            reception_clue_exchange=_as_bool(base.get("reception_clue_exchange"), True),
            reception_send_clue=_as_bool(base.get("reception_send_clue"), True),
            variants=_as_dict_list(base.get("variants"), "base.variants"),
        ),
        mall=MallConfig(
            visit_friends=_as_bool(mall.get("visit_friends"), True),
            shopping=_as_bool(mall.get("shopping"), True),
            buy_first=_as_str_list(mall.get("buy_first")),
            blacklist=_as_str_list(mall.get("blacklist")),
            force_shopping_if_credit_full=_as_bool(
                mall.get("force_shopping_if_credit_full"), False
            ),
            only_buy_discount=_as_bool(mall.get("only_buy_discount"), False),
            reserve_max_credit=_as_bool(mall.get("reserve_max_credit"), False),
            credit_fight=_as_bool(mall.get("credit_fight"), False),
            formation_index=int(mall.get("formation_index", 0)),
        ),
        award=AwardConfig(
            award=_as_bool(award.get("award"), True),
            mail=_as_bool(award.get("mail"), True),
            recruit=_as_bool(award.get("recruit"), False),
            orundum=_as_bool(award.get("orundum"), False),
            mining=_as_bool(award.get("mining"), False),
            specialaccess=_as_bool(award.get("specialaccess"), False),
        ),
        recruit=RecruitConfig(
            enabled=_as_bool(recruit.get("enabled"), True),
            refresh=_as_bool(recruit.get("refresh"), True),
            select=_as_int_list(recruit.get("select"), "recruit.select", [5, 4]),
            confirm=_as_int_list(recruit.get("confirm"), "recruit.confirm", [5, 4]),
            times=int(_as_int(recruit.get("times"), "recruit.times", 4) or 0),
            set_time=_as_bool(recruit.get("set_time"), True),
            expedite=_as_bool(recruit.get("expedite"), True),
            expedite_times=_as_int(recruit.get("expedite_times"), "recruit.expedite_times"),
            skip_robot=_as_bool(recruit.get("skip_robot"), True),
            extra_tags_mode=int(
                _as_int(recruit.get("extra_tags_mode"), "recruit.extra_tags_mode", 0) or 0
            ),
        ),
        schedule=ScheduleConfig(
            enabled=_as_bool(
                _env_first(env, "SCHEDULE_ENABLED"),
                _as_bool(schedule.get("enabled"), False),
            ),
            daily_times=_as_time_list(
                _env_first(env, "SCHEDULE_DAILY_TIMES") or schedule.get("daily_times"),
                "schedule.daily_times",
                ["08:00", "20:00"],
            ),
            timezone=_env_first(env, "SCHEDULE_TIMEZONE")
            or str(schedule.get("timezone", "Asia/Shanghai")),
            notify_chat_id=_as_int(
                _env_first(env, "SCHEDULE_NOTIFY_CHAT_ID") or schedule.get("notify_chat_id"),
                "schedule.notify_chat_id",
            ),
        ),
    )
    return config
