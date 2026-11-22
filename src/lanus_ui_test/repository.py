from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import RuntimeSignal, TaskState, TaskStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRepository:
    """Durable task snapshots and external control signals.

    Every graph node writes a full, schema-validated snapshot. This intentionally
    complements LangGraph's in-process execution: a service restart can rebuild
    the graph from the latest committed snapshot without losing step progress.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    task_id TEXT PRIMARY KEY,
                    pause INTEGER NOT NULL DEFAULT 0,
                    stop INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create(self, state: TaskState) -> None:
        now = utc_now()
        if not state.created_at:
            state.created_at = now
        state.updated_at = now
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks(task_id, status, state_json, created_at, updated_at, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    state.task_id,
                    state.status.value,
                    state.model_dump_json(),
                    state.created_at,
                    state.updated_at,
                    state.error,
                ),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO signals(task_id, pause, stop, updated_at)
                VALUES (?, 0, 0, ?)
                """,
                (state.task_id, now),
            )

    def save(self, state: TaskState) -> None:
        state.updated_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, state_json = ?, updated_at = ?, error = ?
                WHERE task_id = ?
                """,
                (
                    state.status.value,
                    state.model_dump_json(),
                    state.updated_at,
                    state.error,
                    state.task_id,
                ),
            )

    def load(self, task_id: str) -> TaskState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return TaskState.model_validate_json(row["state_json"])

    def get_status(self, task_id: str) -> TaskStatus | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return TaskStatus(row["status"]) if row else None

    def set_signal(
        self,
        task_id: str,
        *,
        pause: bool | None = None,
        stop: bool | None = None,
    ) -> RuntimeSignal:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT pause, stop FROM signals WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            next_pause = int(pause) if pause is not None else int(row["pause"])
            next_stop = int(stop) if stop is not None else int(row["stop"])
            connection.execute(
                """
                UPDATE signals
                SET pause = ?, stop = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (next_pause, next_stop, now, task_id),
            )
        return RuntimeSignal(pause=bool(next_pause), stop=bool(next_stop))

    def get_signal(self, task_id: str) -> RuntimeSignal:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT pause, stop FROM signals WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return RuntimeSignal()
        return RuntimeSignal(pause=bool(row["pause"]), stop=bool(row["stop"]))

    def clear_pause(self, task_id: str) -> None:
        self.set_signal(task_id, pause=False)

    def list_task_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        return [row["task_id"] for row in rows]
