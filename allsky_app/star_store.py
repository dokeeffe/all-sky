from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class StarStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS star_counts (
                    id INTEGER PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    image_path TEXT NOT NULL UNIQUE,
                    star_count INTEGER NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS star_counts_captured_at "
                "ON star_counts(captured_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def add(self, image_path: Path, star_count: int, captured_at: datetime) -> None:
        timestamp = captured_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO star_counts(captured_at, image_path, star_count) "
                "VALUES (?, ?, ?)",
                (timestamp, str(image_path), star_count),
            )

    def recent(self, limit: int = 20) -> list[dict[str, str | int]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT captured_at, image_path, star_count FROM star_counts "
                "ORDER BY captured_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
