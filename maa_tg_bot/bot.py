from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from .auth import is_authorized
from .config import AppConfig, load_config
from .maa_cli import MaaCliExecutor, tail_text
from .maa_summary import build_daily_completion_details
from .models import TaskKind, TaskRequest, TaskResult, TaskState
from .state import BotStateStore
from .task_queue import TaskQueue

logger = logging.getLogger(__name__)
SCHEDULER_TASK_KEY = "daily_scheduler_task"


TASK_KIND_LABELS = {
    TaskKind.FIGHT: "刷理智",
    TaskKind.DAILY: "日常任务",
    TaskKind.SCREENSHOT: "截图",
}

TASK_STATE_LABELS = {
    TaskState.PENDING: "等待中",
    TaskState.RUNNING: "运行中",
    TaskState.SUCCEEDED: "成功",
    TaskState.FAILED: "失败",
    TaskState.CANCELLED: "已取消",
}

HELP_TEXT = """可用命令：
/status - 查看队列和运行状态
/setstage [stage|clear] - 查看、设置或清空默认刷图关卡
/setschedule [HH:MM ...|clear] - 查看、设置或清空定时 daily 时间
/fight [stage] [medicine] [times] - 执行刷理智任务
/daily - 执行日常任务：邮件、信用商店、公开招募、刷理智、基建换班、最后领奖
/screenshot - 获取当前安卓画面截图
/closegame - 关闭明日方舟 App
/stop - 停止当前 maa-cli 任务
/help - 显示帮助
"""

PUBLIC_COMMANDS = (
    BotCommand("status", "查看队列和运行状态"),
    BotCommand("setstage", "设置默认刷图关卡"),
    BotCommand("setschedule", "设置定时 daily 时间"),
    BotCommand("fight", "执行刷理智任务"),
    BotCommand("daily", "执行日常任务"),
    BotCommand("screenshot", "获取当前画面截图"),
    BotCommand("closegame", "关闭明日方舟 App"),
    BotCommand("stop", "停止当前任务"),
    BotCommand("help", "显示帮助"),
)


def task_kind_label(kind: TaskKind) -> str:
    return TASK_KIND_LABELS.get(kind, kind.value)


def task_state_label(state: TaskState) -> str:
    return TASK_STATE_LABELS.get(state, state.value)


def exit_code_label(result) -> str:
    return "超时" if result.timed_out else str(result.exit_code)


def build_application(config: AppConfig) -> Application:
    executor = MaaCliExecutor(config)
    queue = TaskQueue(executor, maxsize=config.bot.queue_size)
    state_store = BotStateStore(
        config.maa.config_dir / "bot-state.sqlite",
        legacy_json_path=config.maa.config_dir / "bot-state.json",
    )

    async def post_init(application: Application) -> None:
        await queue.start()
        application.bot_data["task_queue"] = queue
        application.bot_data["executor"] = executor
        application.bot_data["config"] = config
        application.bot_data["state_store"] = state_store
        await set_public_commands(application)
        await start_daily_scheduler(application)

    async def post_shutdown(application: Application) -> None:
        await stop_daily_scheduler(application)
        await executor.stop_current()
        await queue.stop()

    builder = (
        ApplicationBuilder()
        .token(config.telegram.token)
        .connection_pool_size(16)
        .pool_timeout(30)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .get_updates_connection_pool_size(8)
        .get_updates_pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(60)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if config.telegram.proxy:
        logger.info("使用代理连接 Telegram: %s", config.telegram.proxy)
        builder = builder.proxy(config.telegram.proxy).get_updates_proxy(config.telegram.proxy)
    application = builder.build()
    application.bot_data["task_queue"] = queue
    application.bot_data["executor"] = executor
    application.bot_data["config"] = config
    application.bot_data["state_store"] = state_store

    application.add_handler(CommandHandler(["start", "help"], help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("setstage", setstage_command))
    application.add_handler(CommandHandler("setschedule", setschedule_command))
    application.add_handler(CommandHandler("fight", fight_command))
    application.add_handler(CommandHandler("daily", simple_task_command(TaskKind.DAILY)))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("closegame", closegame_command))
    application.add_handler(CommandHandler("stop", stop_command))
    return application


async def set_public_commands(application: Application) -> None:
    try:
        await application.bot.set_my_commands(PUBLIC_COMMANDS)
    except Exception:
        logger.exception("设置 Telegram Bot 命令菜单失败")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def require_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    config: AppConfig = context.application.bot_data["config"]
    user_id = update.effective_user.id if update.effective_user else None
    if is_authorized(user_id, config.telegram.allowed_user_ids):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("无权限。")
    logger.warning("拒绝未授权用户 user_id=%s", user_id)
    return False


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update, context):
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update, context):
        return
    logger.info("处理 /status chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    config: AppConfig = context.application.bot_data["config"]
    queue: TaskQueue = context.application.bot_data["task_queue"]
    executor: MaaCliExecutor = context.application.bot_data["executor"]
    snapshot = queue.snapshot()
    health = await executor.health()
    store = get_state_store(context.application)
    stage = resolve_fight_stage(config, {}, store) or "未设置"
    daily_times = ", ".join(resolve_daily_times(config, store))
    schedule_state = "启用" if config.schedule.enabled else "未启用"

    running = task_kind_label(snapshot.running.kind) if snapshot.running else "无"
    pending = ", ".join(task_kind_label(item.kind) for item in snapshot.pending) or "无"
    last = "无"
    if snapshot.last_result:
        last = (
            f"{task_kind_label(snapshot.last_result.request.kind)} "
            f"{task_state_label(snapshot.last_result.state)} "
            f"{snapshot.last_result.duration_seconds:.1f} 秒"
        )
    text = (
        f"运行中：{running}\n"
        f"等待中：{pending}\n"
        f"上次任务：{last}\n"
        f"默认关卡：{stage}\n"
        f"定时日常：{schedule_state} {daily_times}\n"
        f"MAA: {health['maa']}\n"
        f"ADB: {health['adb']}\n"
        f"配置：{health['config_dir']} 档案={health['profile']}"
    )
    await update.effective_message.reply_text(text)


async def setstage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update, context):
        return
    config: AppConfig = context.application.bot_data["config"]
    store = get_state_store(context.application)
    args = [arg.strip() for arg in context.args if arg.strip()]
    if not args:
        runtime_state = store.load()
        if runtime_state.updated_at:
            if runtime_state.fight_stage:
                await update.effective_message.reply_text(
                    f"当前默认刷图关卡：{runtime_state.fight_stage}"
                )
            else:
                await update.effective_message.reply_text(
                    "当前未设置默认刷图关卡。用法：/setstage 1-7"
                )
            return
        legacy_stage = str(config.fight.stage).strip()
        if legacy_stage:
            await update.effective_message.reply_text(
                f"当前默认刷图关卡：{legacy_stage}（来自旧配置；建议用 /setstage 设置）"
            )
            return
        await update.effective_message.reply_text(
            "当前未设置默认刷图关卡。用法：/setstage 1-7"
        )
        return

    if len(args) != 1:
        await update.effective_message.reply_text("用法：/setstage [stage|clear]")
        return

    stage = args[0]
    if stage.lower() in {"clear", "reset", "none"}:
        store.clear_fight_stage()
        await update.effective_message.reply_text("已清空默认刷图关卡。")
        return

    store.save_fight_stage(stage)
    await update.effective_message.reply_text(f"已设置默认刷图关卡：{stage}")


async def setschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update, context):
        return
    config: AppConfig = context.application.bot_data["config"]
    store = get_state_store(context.application)
    args = [arg.strip() for arg in context.args if arg.strip()]
    if not args:
        state = store.load()
        source = "SQLite" if state.schedule_daily_times_updated_at else "部署配置"
        times = ", ".join(resolve_daily_times(config, store))
        schedule_state = "启用" if config.schedule.enabled else "未启用"
        await update.effective_message.reply_text(
            f"当前定时 daily 时间：{times}（{source}）\n定时开关：{schedule_state}"
        )
        return

    if len(args) == 1 and args[0].lower() in {"clear", "reset", "none"}:
        store.clear_schedule_daily_times()
        await restart_daily_scheduler(context.application)
        times = ", ".join(resolve_daily_times(config, store))
        await update.effective_message.reply_text(
            f"已清空运行时定时 daily 时间，当前回退为：{times}"
        )
        return

    try:
        times = normalize_schedule_times(args)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    store.save_schedule_daily_times(times)
    await restart_daily_scheduler(context.application)
    text = f"已设置定时 daily 时间：{', '.join(times)}"
    if not config.schedule.enabled:
        text += "\n注意：定时开关当前未启用，需要在部署配置中开启。"
    await update.effective_message.reply_text(text)


async def fight_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update, context):
        return
    try:
        options = parse_fight_args(context.args)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await enqueue_task(update, context, TaskKind.FIGHT, options)


def simple_task_command(kind: TaskKind):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await require_auth(update, context):
            return
        await enqueue_task(update, context, kind, {})

    return handler


async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update, context):
        return
    logger.info(
        "处理 /screenshot chat_id=%s",
        update.effective_chat.id if update.effective_chat else None,
    )
    executor: MaaCliExecutor = context.application.bot_data["executor"]
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    request = TaskRequest(kind=TaskKind.SCREENSHOT, requested_by=user_id, chat_id=chat_id)
    result = await executor.run(request)
    await send_task_result(context, chat_id, result)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update, context):
        return
    queue: TaskQueue = context.application.bot_data["task_queue"]
    stopped = await queue.stop_current()
    text = "已请求停止当前任务。" if stopped else "当前没有运行中的任务。"
    await update.effective_message.reply_text(text)


async def closegame_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_auth(update, context):
        return
    logger.info(
        "处理 /closegame chat_id=%s",
        update.effective_chat.id if update.effective_chat else None,
    )
    queue: TaskQueue = context.application.bot_data["task_queue"]
    executor: MaaCliExecutor = context.application.bot_data["executor"]
    stopped = await queue.stop_current()
    result = await executor.close_game()
    if result.exit_code == 0:
        text = "已停止当前任务并关闭游戏。" if stopped else "已关闭游戏。"
    else:
        prefix = (
            "已停止当前任务，但关闭游戏失败。"
            if stopped
            else "关闭游戏失败。"
        )
        code = exit_code_label(result)
        output = tail_text(result.output.strip(), 1000)
        text = f"{prefix} 退出码={code}"
        if output:
            text += f"\n{output}"
    await update.effective_message.reply_text(text[:3900])


async def enqueue_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: TaskKind,
    options: dict,
) -> None:
    config: AppConfig = context.application.bot_data["config"]
    options = dict(options)
    if kind in {TaskKind.FIGHT, TaskKind.DAILY}:
        stage = resolve_fight_stage(config, options, get_state_store(context.application))
        if not stage:
            await update.effective_message.reply_text(
                "未配置刷图关卡。请使用 /setstage <stage> 设置默认关卡，"
                "或使用 /fight <stage> 指定本次刷图关卡。"
            )
            return
        options.setdefault("stage", stage)

    if kind in {TaskKind.FIGHT, TaskKind.DAILY} and not fight_stage_is_configured(config, options):
        await update.effective_message.reply_text(
            "未配置刷图关卡。请使用 /setstage <stage> 设置默认关卡。"
        )
        return

    queue: TaskQueue = context.application.bot_data["task_queue"]
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    logger.info("排队 /%s chat_id=%s", kind.value, chat_id)

    async def notify(result: TaskResult) -> None:
        await send_task_result(context, chat_id, result)

    request = TaskRequest(kind=kind, requested_by=user_id, chat_id=chat_id, options=options, notify=notify)
    try:
        position = await queue.enqueue(request)
    except asyncio.QueueFull:
        await update.effective_message.reply_text("任务队列已满。")
        return
    await update.effective_message.reply_text(
        f"已排队{task_kind_label(kind)} {request.id}。队列位置：{position}。"
    )


async def send_task_result(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    result: TaskResult,
) -> None:
    await send_task_result_to_bot(context.bot, context.application, chat_id, result)


async def send_task_result_to_bot(
    bot,
    application: Application,
    chat_id: int,
    result: TaskResult,
) -> None:
    await finalize_task_result(application, result)
    summary = (
        f"{task_kind_label(result.request.kind)} {task_state_label(result.state)}\n"
        f"任务 ID：{result.request.id}\n"
        f"耗时：{result.duration_seconds:.1f} 秒\n"
        f"{result.message}"
    )
    if result.request.kind == TaskKind.DAILY and result.state == TaskState.SUCCEEDED:
        detail = build_daily_completion_details(result.output_tail)
        if detail:
            summary += f"\n\n{detail}"
    if result.output_tail and result.state != TaskState.SUCCEEDED:
        summary += f"\n\n日志尾部：\n{tail_text(result.output_tail, 1500)}"
    if result.artifact_path and result.artifact_path.exists():
        with result.artifact_path.open("rb") as file:
            await bot.send_photo(chat_id=chat_id, photo=file, caption=summary[:1024])
        return
    await bot.send_message(chat_id=chat_id, text=summary[:3900])


async def finalize_task_result(application: Application, result: TaskResult) -> None:
    if result.request.kind != TaskKind.DAILY or result.state != TaskState.SUCCEEDED:
        return
    executor: MaaCliExecutor = application.bot_data["executor"]
    close_result = await executor.close_game()
    if close_result.exit_code == 0:
        result.message += "\n日常任务完成后已关闭游戏。"
        return

    code = exit_code_label(close_result)
    text = f"\n日常任务完成后关闭游戏失败。退出码={code}"
    output = tail_text(close_result.output.strip(), 1000)
    if output:
        text += f"\n{output}"
    result.message += text


async def start_daily_scheduler(application: Application) -> None:
    config: AppConfig = application.bot_data["config"]
    if not config.schedule.enabled:
        return
    if config.schedule.notify_chat_id is None:
        logger.warning("定时日常已禁用：[schedule].notify_chat_id 未配置")
        return
    try:
        timezone = ZoneInfo(config.schedule.timezone)
    except ZoneInfoNotFoundError:
        logger.exception("定时日常已禁用：未知时区 %s", config.schedule.timezone)
        return

    task = asyncio.create_task(
        daily_scheduler_loop(application, timezone),
        name="maa-daily-scheduler",
    )
    application.bot_data[SCHEDULER_TASK_KEY] = task


async def stop_daily_scheduler(application: Application) -> None:
    task = application.bot_data.pop(SCHEDULER_TASK_KEY, None)
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def restart_daily_scheduler(application: Application) -> None:
    await stop_daily_scheduler(application)
    await start_daily_scheduler(application)


async def daily_scheduler_loop(application: Application, timezone: ZoneInfo) -> None:
    config: AppConfig = application.bot_data["config"]
    while True:
        now = datetime.now(timezone)
        daily_times = resolve_daily_times(config, get_state_store(application))
        next_run = next_daily_run(now, daily_times)
        wait_seconds = max(0.0, (next_run - now).total_seconds())
        logger.info("下一次定时日常将在 %s 执行", next_run.isoformat())
        await asyncio.sleep(wait_seconds)
        try:
            await queue_scheduled_daily(application)
        except Exception:
            logger.exception("定时日常排队失败")


def next_daily_run(now: datetime, daily_times: list[str]) -> datetime:
    times = sorted(parse_schedule_time(value) for value in daily_times)
    for daily_time in times:
        candidate = now.replace(
            hour=daily_time.hour,
            minute=daily_time.minute,
            second=0,
            microsecond=0,
        )
        if candidate > now:
            return candidate
    first_time = times[0]
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(
        hour=first_time.hour,
        minute=first_time.minute,
        second=0,
        microsecond=0,
    )


def parse_schedule_time(value: str) -> time:
    hour, minute = value.split(":", maxsplit=1)
    return time(hour=int(hour), minute=int(minute))


def normalize_schedule_times(args: list[str]) -> list[str]:
    raw_items: list[str] = []
    for arg in args:
        raw_items.extend(item.strip() for item in arg.split(","))
    items = [item for item in raw_items if item]
    if not items:
        raise ValueError("用法：/setschedule 08:00 20:00")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        try:
            parsed = parse_schedule_time(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("用法：/setschedule 08:00 20:00") from exc
        value = f"{parsed.hour:02d}:{parsed.minute:02d}"
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return sorted(normalized)


async def queue_scheduled_daily(application: Application) -> bool:
    config: AppConfig = application.bot_data["config"]
    chat_id = config.schedule.notify_chat_id
    if chat_id is None:
        logger.warning("跳过定时日常：notify_chat_id 未配置")
        return False
    stage = resolve_fight_stage(config, {}, get_state_store(application))
    if not stage:
        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                "定时日常已跳过：未配置刷图关卡。"
                "请使用 /setstage <stage> 设置默认关卡。"
            ),
        )
        return False

    queue: TaskQueue = application.bot_data["task_queue"]

    async def notify(result: TaskResult) -> None:
        await send_task_result_to_bot(application.bot, application, chat_id, result)

    request = TaskRequest(
        kind=TaskKind.DAILY,
        requested_by=0,
        chat_id=chat_id,
        options={"stage": stage},
        notify=notify,
    )
    try:
        position = await queue.enqueue(request)
    except asyncio.QueueFull:
        await application.bot.send_message(
            chat_id=chat_id,
            text="定时日常已跳过：任务队列已满。",
        )
        return False

    await application.bot.send_message(
        chat_id=chat_id,
        text=f"已排队定时日常任务 {request.id}。队列位置：{position}。",
    )
    return True


def parse_fight_args(args: list[str]) -> dict:
    options: dict[str, str | int] = {}
    if not args:
        return options
    options["stage"] = args[0]
    try:
        if len(args) >= 2:
            options["medicine"] = int(args[1])
        if len(args) >= 3:
            options["times"] = int(args[2])
    except ValueError as exc:
        raise ValueError("用法：/fight [stage] [medicine] [times]") from exc
    return options


def get_state_store(application: Application) -> BotStateStore:
    store = application.bot_data.get("state_store")
    if isinstance(store, BotStateStore):
        return store
    config: AppConfig = application.bot_data["config"]
    store = BotStateStore(
        config.maa.config_dir / "bot-state.sqlite",
        legacy_json_path=config.maa.config_dir / "bot-state.json",
    )
    application.bot_data["state_store"] = store
    return store


def resolve_daily_times(
    config: AppConfig,
    state_store: BotStateStore | None = None,
) -> list[str]:
    if state_store is not None:
        times = state_store.schedule_daily_times()
        if times:
            return list(times)
    return list(config.schedule.daily_times)


def resolve_fight_stage(
    config: AppConfig,
    options: dict,
    state_store: BotStateStore | None = None,
) -> str:
    explicit_stage = str(options.get("stage", "")).strip()
    if explicit_stage:
        return explicit_stage
    if state_store is not None:
        runtime_state = state_store.load()
        if runtime_state.updated_at:
            return runtime_state.fight_stage
    return str(config.fight.stage).strip()


def fight_stage_is_configured(
    config: AppConfig,
    options: dict,
    state_store: BotStateStore | None = None,
) -> bool:
    return bool(resolve_fight_stage(config, options, state_store))


def main() -> None:
    config = load_config()
    setup_logging(config.bot.log_dir)
    logger.info("启动 maa-tg-bot")
    application = build_application(config)
    application.run_polling(allowed_updates=Update.ALL_TYPES)
