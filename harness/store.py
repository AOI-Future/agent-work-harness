"""Durable local journal and exclusive controller transactions."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .workspace import HarnessError


class Store:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise HarnessError("runtime root must be absolute and non-symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve()
        database = self.root / "state.sqlite"
        if database.is_symlink():
            raise HarnessError("runtime database may not be a symlink")
        self.connection = sqlite3.connect(database, timeout=5)
        os.chmod(database, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
                notify INTEGER NOT NULL DEFAULT 0
            );
            /* The event journal is append-only evidence: durable, replayable,
               and tamper-evident. Updates and deletes abort. */
            CREATE TRIGGER IF NOT EXISTS events_append_only_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events journal is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS events_append_only_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events journal is append-only');
            END;
        """)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def get(self, task_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HarnessError("unknown task")
        return dict(json.loads(row["data"]))

    def put(self, task: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (task["spec"]["id"], task["spec_digest"], json.dumps(task, sort_keys=True)),
        )

    def exists(self, task_id: str) -> bool:
        return (
            self.connection.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
            is not None
        )

    def ids(self) -> list[str]:
        return [row[0] for row in self.connection.execute("SELECT id FROM tasks ORDER BY id")]

    def event(
        self, task_id: str, at: float, kind: str, data: dict[str, Any], notify: bool = False
    ) -> None:
        self.connection.execute(
            "INSERT INTO events(task_id,at,kind,data,notify) VALUES(?,?,?,?,?)",
            (task_id, at, kind, json.dumps(data, sort_keys=True), int(notify)),
        )

    def replay(self, task_id: str) -> list[dict[str, Any]]:
        """Replay the full durable audit narrative of a task, in journal order.

        The data column is written only by ``event()`` as canonical JSON; a row
        that fails to parse is evidence of database tampering or corruption, so
        it must abort loudly and name the offending sequence.
        """
        rows = self.connection.execute(
            "SELECT seq, at, kind, data FROM events WHERE task_id=? ORDER BY seq", (task_id,)
        )
        replayed: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["data"])
            except ValueError as exc:
                raise HarnessError(
                    f"event journal corrupt: seq {row['seq']} is not valid JSON"
                ) from exc
            replayed.append(dict(row) | {"data": payload})
        return replayed

    def events(
        self, task_id: str, *, notifications: bool = False, after: int = 0
    ) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE task_id=? AND seq>? AND (?=0 OR notify=1) ORDER BY seq",
            (task_id, after, int(notifications)),
        )
        return [dict(row) | {"data": json.loads(row["data"])} for row in rows]
