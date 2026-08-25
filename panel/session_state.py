from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    valid INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
"""


class SessionStateStore:
    """Tracks the panel's read on the saved broker session (auth_state.json):
    whether it last looked valid, and how many automatic login attempts in a
    row have failed. Shared between the status light's own checks and every
    order run's login, so either can trip the manual-login fallback. A single
    row (id=1) — there is only ever one broker session to track."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO session_state (id, valid, consecutive_failures) VALUES (1, 0, 0)")
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get(self) -> dict:
        conn = self._connect()
        row = conn.execute(
            "SELECT valid, checked_at, consecutive_failures FROM session_state WHERE id = 1"
        ).fetchone()
        conn.close()
        return {
            "valid": bool(row["valid"]),
            "checked_at": row["checked_at"],
            "consecutive_failures": row["consecutive_failures"],
        }

    async def get(self) -> dict:
        return await asyncio.to_thread(self._get)

    def _set_valid(self, valid: bool) -> None:
        conn = self._connect()
        conn.execute(
            "UPDATE session_state SET valid = ?, checked_at = ? WHERE id = 1",
            (1 if valid else 0, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

    async def set_valid(self, valid: bool) -> None:
        """Records the outcome of a plain status check (no login attempt was
        made) — leaves the failure streak untouched, since only an actual
        login attempt should move it."""
        await asyncio.to_thread(self._set_valid, valid)

    def _record_login_success(self) -> None:
        conn = self._connect()
        conn.execute(
            "UPDATE session_state SET valid = 1, checked_at = ?, consecutive_failures = 0 WHERE id = 1",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        conn.close()

    async def record_login_success(self) -> None:
        await asyncio.to_thread(self._record_login_success)

    def _record_login_failure(self) -> int:
        conn = self._connect()
        conn.execute(
            "UPDATE session_state SET valid = 0, checked_at = ?, consecutive_failures = consecutive_failures + 1 "
            "WHERE id = 1",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        row = conn.execute("SELECT consecutive_failures FROM session_state WHERE id = 1").fetchone()
        conn.close()
        return row["consecutive_failures"]

    async def record_login_failure(self) -> int:
        """Returns the new streak length, so a caller can compare it against
        the manual-login threshold without a second round trip."""
        return await asyncio.to_thread(self._record_login_failure)
