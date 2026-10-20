import asyncio
import unittest
from datetime import UTC, datetime

from maa_tg_bot.models import TaskKind, TaskRequest, TaskResult, TaskState
from maa_tg_bot.task_queue import TaskQueue


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.stopped = False
        self.run_started = asyncio.Event()
        self.finish_run = asyncio.Event()

    async def run(self, request: TaskRequest) -> TaskResult:
        self.calls.append(request.id)
        self.run_started.set()
        await asyncio.sleep(0.01)
        now = datetime.now(UTC)
        return TaskResult(
            request=request,
            state=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
            exit_code=0,
            message="ok",
        )

    async def stop_current(self) -> bool:
        self.stopped = True
        return True


class SlowExecutor(FakeExecutor):
    async def run(self, request: TaskRequest) -> TaskResult:
        self.calls.append(request.id)
        self.run_started.set()
        await self.finish_run.wait()
        now = datetime.now(UTC)
        return TaskResult(
            request=request,
            state=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
            exit_code=0,
            message="ok",
        )


class TaskQueueTests(unittest.TestCase):
    def test_queue_runs_tasks_serially_and_records_last_result(self):
        async def scenario():
            executor = FakeExecutor()
            queue = TaskQueue(executor, maxsize=3)
            results = []
            await queue.start()

            async def notify(result):
                results.append(result.request.id)

            first = TaskRequest(TaskKind.FIGHT, requested_by=1, chat_id=1, notify=notify)
            second = TaskRequest(TaskKind.DAILY, requested_by=1, chat_id=1, notify=notify)
            await queue.enqueue(first)
            await queue.enqueue(second)
            await queue._queue.join()
            snapshot = queue.snapshot()
            await queue.stop()
            return executor.calls, results, snapshot

        calls, results, snapshot = asyncio.run(scenario())

        self.assertEqual(calls, results)
        self.assertEqual(snapshot.pending_count, 0)
        self.assertIsNotNone(snapshot.last_result)
        self.assertEqual(snapshot.last_result.state, TaskState.SUCCEEDED)

    def test_stop_current_returns_false_without_running_task(self):
        async def scenario():
            executor = FakeExecutor()
            queue = TaskQueue(executor)
            return await queue.stop_current(), executor.stopped

        stopped, delegated = asyncio.run(scenario())

        self.assertFalse(stopped)
        self.assertFalse(delegated)

    def test_stop_current_delegates_when_task_is_running(self):
        async def scenario():
            executor = SlowExecutor()
            queue = TaskQueue(executor)
            await queue.start()
            await queue.enqueue(TaskRequest(TaskKind.DAILY, requested_by=1, chat_id=1))
            await executor.run_started.wait()

            stopped = await queue.stop_current()
            executor.finish_run.set()
            await queue._queue.join()
            await queue.stop()
            return stopped, executor.stopped

        stopped, delegated = asyncio.run(scenario())

        self.assertTrue(stopped)
        self.assertTrue(delegated)


class TaskQueueNotifyFailureTests(unittest.TestCase):
    def test_worker_survives_notify_failure(self):
        async def scenario():
            executor = FakeExecutor()
            queue = TaskQueue(executor, maxsize=3)
            await queue.start()
            delivered = []
            second_done = asyncio.Event()

            async def broken_notify(result):
                raise RuntimeError("telegram down")

            async def ok_notify(result):
                delivered.append(result.request.id)
                second_done.set()

            await queue.enqueue(
                TaskRequest(kind=TaskKind.DAILY, requested_by=0, chat_id=1, id="first",
                            notify=broken_notify)
            )
            await queue.enqueue(
                TaskRequest(kind=TaskKind.DAILY, requested_by=0, chat_id=1, id="second",
                            notify=ok_notify)
            )
            with self.assertLogs("maa_tg_bot.task_queue", level="ERROR"):
                await asyncio.wait_for(second_done.wait(), timeout=5)
            worker_alive = not queue._worker_task.done()
            await queue.stop()
            return executor.calls, delivered, worker_alive

        calls, delivered, worker_alive = asyncio.run(scenario())
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(delivered, ["second"])
        self.assertTrue(worker_alive)


if __name__ == "__main__":
    unittest.main()
