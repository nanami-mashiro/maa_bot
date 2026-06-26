from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


FIGHT_STAGE_KEY = "fight_stage"
SCHEDULE_DAILY_TIMES_KEY = "schedule_daily_times"


@dataclass(frozen=True)
class BotState:
    fight_stage: str = ""
    updated_at: str = ""
    schedule_daily_times: tuple[str, ...] = ()
    schedule_daily_times_updated_at: str = ""


class BotStateStore:
    def __init__(self, path: Path, legacy_json_path: Path | None = None):
        self.path = path
        self.legacy_json_path = legacy_json_path
        self._init_db()
        self._migrate_legacy_json()

    def load(self) -> BotState:
        fight_stage, fight_stage_updated_at = self._get_setting(FIGHT_STAGE_KEY)
        schedule_value, schedule_updated_at = self._get_setting(SCHEDULE_DAILY_TIMES_KEY)
        schedule_times = self._decode_schedule_times(schedule_value)
        return BotState(
            fight_stage=fight_stage.strip(),
            updated_at=fight_stage_updated_at,
            schedule_daily_times=tuple(schedule_times),
            schedule_daily_times_updated_at=schedule_updated_at,
        )

    def fight_stage(self) -> str:
        return self.load().fight_stage

    def save_fight_stage(self, stage: str) -> BotState:
        self._put_setting(FIGHT_STAGE_KEY, stage.strip())
        return self.load()

    def clear_fight_stage(self) -> BotState:
        self._put_setting(FIGHT_STAGE_KEY, "")
        return self.load()

    def schedule_daily_times(self) -> tuple[str, ...]:
        return self.load().schedule_daily_times

    def save_schedule_daily_times(self, times: list[str] | tuple[str, ...]) -> BotState:
        self._put_setting(SCHEDULE_DAILY_TIMES_KEY, json.dumps(list(times), ensure_ascii=False))
        return self.load()

    def clear_schedule_daily_times(self) -> BotState:
        self._delete_setting(SCHEDULE_DAILY_TIMES_KEY)
        return self.load()

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _migrate_legacy_json(self) -> None:
        if self.legacy_json_path is None or not self.legacy_json_path.exists():
            return
        existing_stage, _updated_at = self._get_setting(FIGHT_STAGE_KEY)
        if existing_stage:
            return
        try:
            data = json.loads(self.legacy_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        if "fight_stage" not in data:
            return
        stage = str(data.get("fight_stage", "")).strip()
        updated_at = str(data.get("updated_at", "")).strip() or self._now()
        self._put_setting(FIGHT_STAGE_KEY, stage, updated_at=updated_at)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _get_setting(self, key: str) -> tuple[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value, updated_at FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return "", ""
        return str(row[0]), str(row[1])

    def _put_setting(self, key: str, value: str, *, updated_at: str | None = None) -> None:
        timestamp = updated_at or self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, timestamp),
            )

    def _delete_setting(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM settings WHERE key = ?", (key,))

    @staticmethod
    def _decode_schedule_times(value: str) -> list[str]:
        if not value:
            return []
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
