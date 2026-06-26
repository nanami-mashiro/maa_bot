from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol

from .models import QueueSnapshot, TaskRequest, TaskResult, TaskState


class TaskExecutor(Protocol):
    async def run(self, request: TaskRequest) -> TaskResult:
        ...

    async def stop_current(self) -> bool:
        ...


class TaskQueue:
    def __init__(self, executor: TaskExecutor, maxsize: int = 20):
        self.executor = executor
        self._queue: asyncio.Queue[TaskRequest | None] = asyncio.Queue(maxsize=maxsize)
        self._worker_task: asyncio.Task[None] | None = None
        self._running: TaskRequest | None = None
        self._last_result: TaskResult | None = None

    async def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="maa-task-worker")

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
        await self._queue.put(None)
        await self._worker_task
        self._worker_task = None

    async def enqueue(self, request: TaskRequest) -> int:
        self._queue.put_nowait(request)
        return self._queue.qsize()

    async def stop_current(self) -> bool:
        if self._running is None:
            return False
        await self.executor.stop_current()
        return True

    def snapshot(self) -> QueueSnapshot:
        pending = [item for item in list(self._queue._queue) if item is not None]
        return QueueSnapshot(running=self._running, pending=pending, last_result=self._last_result)

    async def _worker(self) -> None:
        while True:
            request = await self._queue.get()
            if request is None:
                self._queue.task_done()
                return

            self._running = request
            started_at = datetime.now(UTC)
            try:
                result = await self.executor.run(request)
            except Exception as exc:
                result = TaskResult(
                    request=request,
                    state=TaskState.FAILED,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    message=f"未处理的任务异常：{exc}",
                )
            finally:
                self._running = None
                self._queue.task_done()

            self._last_result = result
            if request.notify:
                await request.notify(result)
