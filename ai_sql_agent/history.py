"""Durable query metadata scoped by owner and database connection."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    id: int
    created_at: datetime
    question: str
    sql: str
    row_count: int
    elapsed_ms: float
    truncated: bool
    insights: str
    chart: dict[str, str] | None
    attempts: int


class HistoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self.path.is_symlink():
                raise ValueError("Choose a regular file for the history database.") from None
        else:
            os.close(descriptor)

        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS query_history (
                    id INTEGER PRIMARY KEY,
                    owner TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    question TEXT NOT NULL,
                    sql TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    elapsed_ms REAL NOT NULL,
                    truncated INTEGER NOT NULL,
                    insights TEXT NOT NULL,
                    chart TEXT,
                    attempts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS query_history_scope
                    ON query_history (owner, namespace, id DESC);
                """
            )

    def add(
        self,
        owner: str,
        namespace: str,
        *,
        question: str,
        sql: str,
        row_count: int,
        elapsed_ms: float,
        truncated: bool,
        insights: str = "",
        chart: Mapping[str, str] | None = None,
        attempts: int = 1,
    ) -> int:
        self._validate_scope(owner, namespace)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO query_history (
                    owner, namespace, created_at, question, sql, row_count,
                    elapsed_ms, truncated, insights, chart, attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner,
                    namespace,
                    timestamp,
                    question,
                    sql,
                    row_count,
                    elapsed_ms,
                    truncated,
                    insights,
                    json.dumps(dict(chart), ensure_ascii=False) if chart is not None else None,
                    attempts,
                ),
            )
            return int(cursor.lastrowid)

    def list(self, owner: str, namespace: str, *, limit: int = 50) -> list[HistoryEntry]:
        self._validate_scope(owner, namespace)
        if not 1 <= limit <= 200:
            raise ValueError("History limit must be between 1 and 200.")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, question, sql, row_count, elapsed_ms,
                       truncated, insights, chart, attempts
                FROM query_history
                WHERE owner = ? AND namespace = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (owner, namespace, limit),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def get(self, owner: str, namespace: str, entry_id: int) -> HistoryEntry | None:
        self._validate_scope(owner, namespace)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, created_at, question, sql, row_count, elapsed_ms,
                       truncated, insights, chart, attempts
                FROM query_history
                WHERE owner = ? AND namespace = ? AND id = ?
                """,
                (owner, namespace, entry_id),
            ).fetchone()
        return self._entry(row) if row else None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _validate_scope(owner: str, namespace: str) -> None:
        if not owner or not namespace:
            raise ValueError("Both a history owner and database namespace are required.")

    @staticmethod
    def _entry(row: sqlite3.Row) -> HistoryEntry:
        return HistoryEntry(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            question=row["question"],
            sql=row["sql"],
            row_count=row["row_count"],
            elapsed_ms=row["elapsed_ms"],
            truncated=bool(row["truncated"]),
            insights=row["insights"],
            chart=json.loads(row["chart"]) if row["chart"] is not None else None,
            attempts=row["attempts"],
        )
