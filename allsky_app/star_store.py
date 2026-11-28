from __future__ import annotations

import sqlite3
import statistics
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path


def filter_star_count_outliers(
    series: list[dict[str, str | int]],
    window_size: int = 10,
    minimum_history: int = 5,
    sigma_limit: float = 3.0,
) -> list[dict[str, str | int]]:
    """Omit counts beyond three standard deviations of recent accepted frames."""
    history: deque[int] = deque(maxlen=window_size)
    filtered: list[dict[str, str | int]] = []
    for point in series:
        count = int(point["star_count"])
        if len(history) >= minimum_history:
            mean = statistics.fmean(history)
            deviation = max(statistics.pstdev(history), 1.0)
            if abs(count - mean) > sigma_limit * deviation:
                continue
        filtered.append(point)
        history.append(count)
    return filtered


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

    def latest(self) -> dict[str, str | int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT captured_at, image_path, star_count FROM star_counts "
                "ORDER BY captured_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def since(self, hours: float = 6) -> list[dict[str, str | int]]:
        if hours <= 0:
            raise ValueError("Time-series window must be positive")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
            timespec="seconds"
        )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT captured_at, image_path, star_count FROM star_counts "
                "WHERE captured_at >= ? ORDER BY captured_at ASC, id ASC",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]
