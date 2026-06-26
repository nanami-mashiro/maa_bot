import asyncio
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path

if "telegram" not in sys.modules:
    telegram_module = types.ModuleType("telegram")
    class FakeBotCommand:
        def __init__(self, command: str, description: str):
            self.command = command
            self.description = description

    telegram_module.BotCommand = FakeBotCommand
    telegram_module.Update = object
    sys.modules["telegram"] = telegram_module

if "telegram.ext" not in sys.modules:
    telegram_ext_module = types.ModuleType("telegram.ext")
    telegram_ext_module.Application = object
    telegram_ext_module.ApplicationBuilder = object
    telegram_ext_module.CommandHandler = object
    telegram_ext_module.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["telegram.ext"] = telegram_ext_module

from maa_tg_bot.bot import (
    HELP_TEXT,
    PUBLIC_COMMANDS,
    closegame_command,
    enqueue_task,
    fight_stage_is_configured,
    next_daily_run,
    normalize_schedule_times,
    queue_scheduled_daily,
    resolve_daily_times,
    setschedule_command,
    setstage_command,
    screenshot_command,
    send_task_result_to_bot,
    set_public_commands,
    status_command,
)
from maa_tg_bot.config import load_config
from maa_tg_bot.maa_cli import ProcessResult
from maa_tg_bot.models import QueueSnapshot, TaskKind, TaskRequest, TaskResult, TaskState
from maa_tg_bot.state import BotStateStore


DAILY_SUMMARY = """\
Summary
----------------------------------------
[Start game] 01:00:00 - 01:00:02 (2s) Completed
----------------------------------------
[Fight] 01:00:02 - 01:01:00 (58s) Completed
Fight MT-10 2 times, drops:
total drops: 化合切削液 × 1
----------------------------------------
[Base shift] 01:01:00 - 01:02:00 (1m) Completed
Mfg(CombatRecord) with operators: unknown
Trade(Money) with operators: unknown
"""


def test_config():
    return load_config(
        path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
            "MAA_DEFAULT_STAGE": "1-7",
        },
    )


def test_config_without_stage():
    return load_config(
        path=Path("/tmp/nonexistent-maa-tg-bot.toml"),
        env={
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
        },
    )


def test_config_with_schedule(stage: str = "1-7"):
    env = {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_ALLOWED_USER_IDS": "123",
        "SCHEDULE_ENABLED": "true",
        "SCHEDULE_NOTIFY_CHAT_ID": "456",
    }
    if stage:
        env["MAA_DEFAULT_STAGE"] = stage
    return load_config(path=Path("/tmp/nonexistent-maa-tg-bot.toml"), env=env)


class FakeUser:
    def __init__(self, user_id: int = 123):
        self.id = user_id


class FakeChat:
    id = 456


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user_id: int = 123):
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat()
        self.effective_message = FakeMessage()


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []
        self.commands = None

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    async def set_my_commands(self, commands) -> None:
        self.commands = tuple(commands)


class FakeApplication:
    def __init__(self, bot_data):
        self.bot_data = bot_data
        self.bot = FakeBot()


class FakeContext:
    def __init__(self, bot_data, args=None):
        self.application = FakeApplication(bot_data)
        self.bot = FakeBot()
        self.args = list(args or [])


class FakeExecutor:
    def __init__(self):
        self.requests = []
        self.close_game_calls = 0
        self.close_game_result = ProcessResult(exit_code=0, output="")

    async def run(self, request):
        self.requests.append(request)
        now = datetime.now(UTC)
        return TaskResult(
            request=request,
            state=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
            exit_code=0,
            message="截图已完成",
        )

    async def close_game(self):
        self.close_game_calls += 1
        return self.close_game_result

    async def health(self):
        return {
            "maa": "退出码=0 maa ok",
            "adb": "退出码=0 device",
            "config_dir": "/data/maa-config",
            "profile": "default",
        }


class FailingQueue:
    async def enqueue(self, _request):
        raise AssertionError("screenshot should not be queued")


class RecordingQueue:
    def __init__(self):
        self.requests = []

    async def enqueue(self, request):
        self.requests.append(request)
        return len(self.requests)


class FullQueue:
    async def enqueue(self, _request):
        raise asyncio.QueueFull


class StopRecordingQueue:
    def __init__(self, stopped: bool):
        self.stopped = stopped
        self.stop_calls = 0

    async def stop_current(self):
        self.stop_calls += 1
        return self.stopped


class SnapshotQueue:
    def snapshot(self):
        now = datetime.now(UTC)
        return QueueSnapshot(
            running=TaskRequest(TaskKind.DAILY, requested_by=1, chat_id=1),
            pending=[TaskRequest(TaskKind.FIGHT, requested_by=1, chat_id=1)],
            last_result=TaskResult(
                request=TaskRequest(TaskKind.SCREENSHOT, requested_by=1, chat_id=1),
                state=TaskState.SUCCEEDED,
                started_at=now,
                finished_at=now,
            ),
        )


class BotCommandTests(unittest.TestCase):
    def test_help_text_only_lists_public_commands(self):
        self.assertIn("可用命令", HELP_TEXT)
        self.assertIn("查看队列和运行状态", HELP_TEXT)
        for command in (
            "/status",
            "/setstage",
            "/setschedule",
            "/fight",
            "/daily",
            "/screenshot",
            "/closegame",
            "/stop",
            "/help",
        ):
            self.assertIn(command, HELP_TEXT)
        for command in ("/base", "/credit", "/award"):
            self.assertNotIn(command, HELP_TEXT)

    def test_set_public_commands_uses_public_menu(self):
        async def scenario():
            application = FakeApplication({})
            application.bot = FakeBot()

            await set_public_commands(application)

            return [command.command for command in application.bot.commands]

        commands = asyncio.run(scenario())

        self.assertEqual(
            commands,
            [
                "status",
                "setstage",
                "setschedule",
                "fight",
                "daily",
                "screenshot",
                "closegame",
                "stop",
                "help",
            ],
        )

    def test_public_command_descriptions_are_chinese(self):
        descriptions = [command.description for command in PUBLIC_COMMANDS]

        self.assertIn("查看队列和运行状态", descriptions)
        self.assertIn("设置默认刷图关卡", descriptions)
        self.assertIn("设置定时 daily 时间", descriptions)
        self.assertIn("执行日常任务", descriptions)
        self.assertNotIn("show help", descriptions)

    def test_status_text_is_chinese(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                context = FakeContext(
                    {
                        "config": test_config(),
                        "state_store": BotStateStore(Path(tmp) / "bot-state.sqlite"),
                        "executor": FakeExecutor(),
                        "task_queue": SnapshotQueue(),
                    }
                )
                update = FakeUpdate()

                await status_command(update, context)

                return update.effective_message.replies

        replies = asyncio.run(scenario())

        self.assertIn("运行中：日常任务", replies[0])
        self.assertIn("等待中：刷理智", replies[0])
        self.assertIn("上次任务：截图 成功", replies[0])
        self.assertIn("默认关卡：1-7", replies[0])
        self.assertIn("定时日常：未启用 08:00, 20:00", replies[0])
        self.assertIn("配置：/data/maa-config 档案=default", replies[0])

    def test_setstage_sets_shows_and_clears_runtime_stage(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = BotStateStore(Path(tmp) / "bot-state.sqlite")
                bot_data = {
                    "config": test_config_without_stage(),
                    "state_store": store,
                    "task_queue": RecordingQueue(),
                }

                set_update = FakeUpdate()
                await setstage_command(set_update, FakeContext(bot_data, args=["1-7"]))

                show_update = FakeUpdate()
                await setstage_command(show_update, FakeContext(bot_data))

                clear_update = FakeUpdate()
                await setstage_command(clear_update, FakeContext(bot_data, args=["clear"]))

                return (
                    set_update.effective_message.replies,
                    show_update.effective_message.replies,
                    clear_update.effective_message.replies,
                    store.fight_stage(),
                )

        set_replies, show_replies, clear_replies, stage = asyncio.run(scenario())

        self.assertEqual(set_replies, ["已设置默认刷图关卡：1-7"])
        self.assertEqual(show_replies, ["当前默认刷图关卡：1-7"])
        self.assertEqual(clear_replies, ["已清空默认刷图关卡。"])
        self.assertEqual(stage, "")

    def test_setschedule_sets_shows_and_clears_runtime_times(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = BotStateStore(Path(tmp) / "bot-state.sqlite")
                bot_data = {
                    "config": test_config_without_stage(),
                    "state_store": store,
                    "task_queue": RecordingQueue(),
                }

                set_update = FakeUpdate()
                await setschedule_command(
                    set_update,
                    FakeContext(bot_data, args=["20:00", "08:00"]),
                )

                show_update = FakeUpdate()
                await setschedule_command(show_update, FakeContext(bot_data))

                clear_update = FakeUpdate()
                await setschedule_command(clear_update, FakeContext(bot_data, args=["clear"]))

                return (
                    set_update.effective_message.replies,
                    show_update.effective_message.replies,
                    clear_update.effective_message.replies,
                    store.schedule_daily_times(),
                )

        set_replies, show_replies, clear_replies, times = asyncio.run(scenario())

        self.assertIn("已设置定时 daily 时间：08:00, 20:00", set_replies[0])
        self.assertIn("定时开关当前未启用", set_replies[0])
        self.assertIn("当前定时 daily 时间：08:00, 20:00（SQLite）", show_replies[0])
        self.assertEqual(clear_replies, ["已清空运行时定时 daily 时间，当前回退为：08:00, 20:00"])
        self.assertEqual(times, ())

    def test_schedule_times_use_runtime_store_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BotStateStore(Path(tmp) / "bot-state.sqlite")
            store.save_schedule_daily_times(["09:30", "21:30"])

            times = resolve_daily_times(test_config_with_schedule(), store)

        self.assertEqual(times, ["09:30", "21:30"])

    def test_normalize_schedule_times_accepts_space_or_comma_separated_values(self):
        self.assertEqual(
            normalize_schedule_times(["20:00,08:00", "08:00"]),
            ["08:00", "20:00"],
        )
        with self.assertRaises(ValueError):
            normalize_schedule_times(["25:00"])

    def test_screenshot_runs_immediately_without_queue(self):
        async def scenario():
            executor = FakeExecutor()
            context = FakeContext(
                {
                    "config": test_config(),
                    "executor": executor,
                    "task_queue": FailingQueue(),
                }
            )
            update = FakeUpdate()

            await screenshot_command(update, context)

            return executor.requests, context.bot.messages

        requests, messages = asyncio.run(scenario())

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].kind.value, "screenshot")
        self.assertEqual(messages[0][0], FakeChat.id)
        self.assertIn("截图 成功", messages[0][1])

    def test_daily_queues_without_extra_account_state(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                queue = RecordingQueue()
                context = FakeContext(
                    {
                        "config": test_config(),
                        "state_store": BotStateStore(Path(tmp) / "bot-state.sqlite"),
                        "task_queue": queue,
                    }
                )
                update = FakeUpdate()

                await enqueue_task(update, context, TaskKind.DAILY, {})

                return queue.requests, update.effective_message.replies

        requests, replies = asyncio.run(scenario())

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].kind, TaskKind.DAILY)
        self.assertTrue(replies)
        self.assertIn("已排队日常任务", replies[0])

    def test_daily_uses_runtime_stage_without_file_stage(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = BotStateStore(Path(tmp) / "bot-state.sqlite")
                store.save_fight_stage("1-7")
                queue = RecordingQueue()
                context = FakeContext(
                    {
                        "config": test_config_without_stage(),
                        "state_store": store,
                        "task_queue": queue,
                    }
                )
                update = FakeUpdate()

                await enqueue_task(update, context, TaskKind.DAILY, {})

                return queue.requests, update.effective_message.replies

        requests, replies = asyncio.run(scenario())

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].options["stage"], "1-7")
        self.assertIn("已排队日常任务", replies[0])

    def test_fight_explicit_stage_does_not_update_runtime_stage(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = BotStateStore(Path(tmp) / "bot-state.sqlite")
                store.save_fight_stage("1-7")
                queue = RecordingQueue()
                context = FakeContext(
                    {
                        "config": test_config_without_stage(),
                        "state_store": store,
                        "task_queue": queue,
                    }
                )
                update = FakeUpdate()

                await enqueue_task(update, context, TaskKind.FIGHT, {"stage": "CE-6"})

                return queue.requests, store.fight_stage()

        requests, stage = asyncio.run(scenario())

        self.assertEqual(requests[0].options["stage"], "CE-6")
        self.assertEqual(stage, "1-7")

    def test_fight_and_daily_require_stage_source(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                queue = RecordingQueue()
                context = FakeContext(
                    {
                        "config": test_config_without_stage(),
                        "state_store": BotStateStore(Path(tmp) / "bot-state.sqlite"),
                        "task_queue": queue,
                    }
                )
                update = FakeUpdate()

                await enqueue_task(update, context, TaskKind.DAILY, {})

                return queue.requests, update.effective_message.replies

        requests, replies = asyncio.run(scenario())

        self.assertEqual(requests, [])
        self.assertIn("未配置刷图关卡", replies[0])
        self.assertFalse(fight_stage_is_configured(test_config_without_stage(), {}))
        self.assertTrue(fight_stage_is_configured(test_config_without_stage(), {"stage": "CE-6"}))

    def test_closegame_stops_task_then_closes_game(self):
        async def scenario():
            queue = StopRecordingQueue(stopped=True)
            executor = FakeExecutor()
            context = FakeContext(
                {
                    "config": test_config(),
                    "executor": executor,
                    "task_queue": queue,
                }
            )
            update = FakeUpdate()

            await closegame_command(update, context)

            return queue.stop_calls, executor.close_game_calls, update.effective_message.replies

        stop_calls, close_game_calls, replies = asyncio.run(scenario())

        self.assertEqual(stop_calls, 1)
        self.assertEqual(close_game_calls, 1)
        self.assertEqual(replies, ["已停止当前任务并关闭游戏。"])

    def test_closegame_rejects_unauthorized_user(self):
        async def scenario():
            queue = StopRecordingQueue(stopped=True)
            executor = FakeExecutor()
            context = FakeContext(
                {
                    "config": test_config(),
                    "executor": executor,
                    "task_queue": queue,
                }
            )
            update = FakeUpdate(user_id=999)

            await closegame_command(update, context)

            return queue.stop_calls, executor.close_game_calls, update.effective_message.replies

        stop_calls, close_game_calls, replies = asyncio.run(scenario())

        self.assertEqual(stop_calls, 0)
        self.assertEqual(close_game_calls, 0)
        self.assertEqual(replies, ["无权限。"])

    def test_daily_success_closes_game_before_sending_result(self):
        async def scenario():
            executor = FakeExecutor()
            application = FakeApplication({"executor": executor})
            bot = FakeBot()
            now = datetime.now(UTC)
            result = TaskResult(
                request=TaskRequest(TaskKind.DAILY, requested_by=123, chat_id=456),
                state=TaskState.SUCCEEDED,
                started_at=now,
                finished_at=now,
                exit_code=0,
                message="任务已完成",
                output_tail=DAILY_SUMMARY,
            )

            await send_task_result_to_bot(bot, application, 456, result)

            return executor.close_game_calls, bot.messages, result.state

        close_game_calls, messages, state = asyncio.run(scenario())

        self.assertEqual(close_game_calls, 1)
        self.assertEqual(state, TaskState.SUCCEEDED)
        self.assertIn("日常任务完成后已关闭游戏。", messages[0][1])
        self.assertIn("完成明细：", messages[0][1])
        self.assertIn("刷理智：MT-10 × 2，掉落：化合切削液 × 1", messages[0][1])
        self.assertIn("基建换班：制造站 1 个（作战记录 1），贸易站 1 个（龙门币 1）", messages[0][1])

    def test_daily_failure_does_not_close_game(self):
        async def scenario():
            executor = FakeExecutor()
            application = FakeApplication({"executor": executor})
            bot = FakeBot()
            now = datetime.now(UTC)
            result = TaskResult(
                request=TaskRequest(TaskKind.DAILY, requested_by=123, chat_id=456),
                state=TaskState.FAILED,
                started_at=now,
                finished_at=now,
                exit_code=1,
                message="任务失败",
            )

            await send_task_result_to_bot(bot, application, 456, result)

            return executor.close_game_calls, bot.messages

        close_game_calls, messages = asyncio.run(scenario())

        self.assertEqual(close_game_calls, 0)
        self.assertNotIn("日常任务完成后已关闭游戏。", messages[0][1])

    def test_daily_close_game_failure_is_appended_without_failing_daily(self):
        async def scenario():
            executor = FakeExecutor()
            executor.close_game_result = ProcessResult(exit_code=1, output="adb failed")
            application = FakeApplication({"executor": executor})
            bot = FakeBot()
            now = datetime.now(UTC)
            result = TaskResult(
                request=TaskRequest(TaskKind.DAILY, requested_by=123, chat_id=456),
                state=TaskState.SUCCEEDED,
                started_at=now,
                finished_at=now,
                exit_code=0,
                message="任务已完成",
            )

            await send_task_result_to_bot(bot, application, 456, result)

            return executor.close_game_calls, bot.messages, result.state

        close_game_calls, messages, state = asyncio.run(scenario())

        self.assertEqual(close_game_calls, 1)
        self.assertEqual(state, TaskState.SUCCEEDED)
        self.assertIn("日常任务完成后关闭游戏失败。退出码=1", messages[0][1])
        self.assertIn("adb failed", messages[0][1])

    def test_queue_scheduled_daily_enqueues_and_notifies(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                queue = RecordingQueue()
                application = FakeApplication(
                    {
                        "config": test_config_with_schedule(),
                        "state_store": BotStateStore(Path(tmp) / "bot-state.sqlite"),
                        "task_queue": queue,
                        "executor": FakeExecutor(),
                    }
                )

                queued = await queue_scheduled_daily(application)

                return queued, queue.requests, application.bot.messages

        queued, requests, messages = asyncio.run(scenario())

        self.assertTrue(queued)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].kind, TaskKind.DAILY)
        self.assertEqual(requests[0].options["stage"], "1-7")
        self.assertEqual(messages[0][0], 456)
        self.assertIn("已排队定时日常任务", messages[0][1])

    def test_queue_scheduled_daily_uses_runtime_stage(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = BotStateStore(Path(tmp) / "bot-state.sqlite")
                store.save_fight_stage("CE-6")
                queue = RecordingQueue()
                application = FakeApplication(
                    {
                        "config": test_config_with_schedule(stage=""),
                        "state_store": store,
                        "task_queue": queue,
                        "executor": FakeExecutor(),
                    }
                )

                queued = await queue_scheduled_daily(application)

                return queued, queue.requests

        queued, requests = asyncio.run(scenario())

        self.assertTrue(queued)
        self.assertEqual(requests[0].options["stage"], "CE-6")

    def test_queue_scheduled_daily_skips_without_stage(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                queue = RecordingQueue()
                application = FakeApplication(
                    {
                        "config": test_config_with_schedule(stage=""),
                        "state_store": BotStateStore(Path(tmp) / "bot-state.sqlite"),
                        "task_queue": queue,
                        "executor": FakeExecutor(),
                    }
                )

                queued = await queue_scheduled_daily(application)

                return queued, queue.requests, application.bot.messages

        queued, requests, messages = asyncio.run(scenario())

        self.assertFalse(queued)
        self.assertEqual(requests, [])
        self.assertIn("定时日常已跳过：未配置刷图关卡", messages[0][1])

    def test_queue_scheduled_daily_skips_when_queue_full(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                application = FakeApplication(
                    {
                        "config": test_config_with_schedule(),
                        "state_store": BotStateStore(Path(tmp) / "bot-state.sqlite"),
                        "task_queue": FullQueue(),
                        "executor": FakeExecutor(),
                    }
                )

                queued = await queue_scheduled_daily(application)

                return queued, application.bot.messages

        queued, messages = asyncio.run(scenario())

        self.assertFalse(queued)
        self.assertIn("定时日常已跳过：任务队列已满。", messages[0][1])

    def test_next_daily_run_uses_next_configured_time(self):
        now = datetime(2026, 5, 26, 8, 30, tzinfo=UTC)

        self.assertEqual(
            next_daily_run(now, ["08:00", "20:00"]),
            datetime(2026, 5, 26, 20, 0, tzinfo=UTC),
        )
        self.assertEqual(
            next_daily_run(datetime(2026, 5, 26, 20, 0, tzinfo=UTC), ["08:00", "20:00"]),
            datetime(2026, 5, 27, 8, 0, tzinfo=UTC),
        )


if __name__ == "__main__":
    unittest.main()
