from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable


class TaskKind(str, Enum):
    FIGHT = "fight"
    DAILY = "daily"
    SCREENSHOT = "screenshot"


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


NotifyCallback = Callable[["TaskResult"], Awaitable[None]]


@dataclass
class TaskRequest:
    kind: TaskKind
    requested_by: int
    chat_id: int
    options: dict[str, Any] = field(default_factory=dict)
    notify: NotifyCallback | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class TaskResult:
    request: TaskRequest
    state: TaskState
    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    message: str = ""
    output_tail: str = ""
    artifact_path: Path | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True)
class QueueSnapshot:
    running: TaskRequest | None
    pending: list[TaskRequest]
    last_result: TaskResult | None

    @property
    def pending_count(self) -> int:
        return len(self.pending)
