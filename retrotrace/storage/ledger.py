"""
retrotrace/storage/ledger.py
Append-only SQLite ledger for execution events, backed by Pydantic v2 models.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, UUID4


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------

class ExecutionEvent(BaseModel):
    """Immutable record of a single traced function call."""

    event_id: str = Field(..., description="UUID4 uniquely identifying this event")
    trace_id: str = Field(..., description="UUID4 grouping all events of one run")
    parent_id: Optional[str] = Field(None, description="event_id of the caller, if any")
    function_name: str
    module_path: str
    inputs: Dict[str, Any] = Field(..., description="Serialized function arguments")
    output: Optional[Any] = Field(None, description="Serialized return value")
    error: Optional[str] = Field(None, description="Exception message, if the call failed")
    started_at: datetime
    completed_at: datetime
    duration_ms: float

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS execution_events (
    event_id        TEXT NOT NULL,
    trace_id        TEXT NOT NULL,
    parent_id       TEXT,
    function_name   TEXT NOT NULL,
    module_path     TEXT NOT NULL,
    inputs          TEXT NOT NULL,   -- JSON
    output          TEXT,            -- JSON (nullable)
    error           TEXT,
    started_at      TEXT NOT NULL,   -- ISO-8601
    completed_at    TEXT NOT NULL,   -- ISO-8601
    duration_ms     REAL NOT NULL,
    PRIMARY KEY (event_id)
);
"""

_CREATE_IDX_TRACE = (
    "CREATE INDEX IF NOT EXISTS idx_execution_events_trace_id "
    "ON execution_events (trace_id);"
)

_CREATE_IDX_EVENT = (
    "CREATE INDEX IF NOT EXISTS idx_execution_events_event_id "
    "ON execution_events (event_id);"
)

_INSERT = """
INSERT INTO execution_events
    (event_id, trace_id, parent_id, function_name, module_path,
     inputs, output, error, started_at, completed_at, duration_ms)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _dt_to_str(dt: datetime) -> str:
    """Store datetimes as UTC ISO-8601 strings."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _str_to_dt(s: str) -> datetime:
    """Restore datetimes from ISO-8601 strings."""
    # Python 3.7+ fromisoformat handles the Z suffix only from 3.11 onward.
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _event_to_row(event: ExecutionEvent) -> tuple:
    return (
        event.event_id,
        event.trace_id,
        event.parent_id,
        event.function_name,
        event.module_path,
        json.dumps(event.inputs),
        json.dumps(event.output) if event.output is not None else None,
        event.error,
        _dt_to_str(event.started_at),
        _dt_to_str(event.completed_at),
        event.duration_ms,
    )


def _row_to_event(row: sqlite3.Row) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=row["event_id"],
        trace_id=row["trace_id"],
        parent_id=row["parent_id"],
        function_name=row["function_name"],
        module_path=row["module_path"],
        inputs=json.loads(row["inputs"]),
        output=json.loads(row["output"]) if row["output"] is not None else None,
        error=row["error"],
        started_at=_str_to_dt(row["started_at"]),
        completed_at=_str_to_dt(row["completed_at"]),
        duration_ms=row["duration_ms"],
    )


# ---------------------------------------------------------------------------
# ExecutionLedger
# ---------------------------------------------------------------------------

class ExecutionLedger:
    """
    Append-only ledger that persists :class:`ExecutionEvent` objects to SQLite.

    Thread-safe: each call acquires the lock and uses its own cursor through
    the shared ``check_same_thread=False`` connection.
    """

    def __init__(self, db_path: str | Path = ".retrotrace.db") -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._initialise_schema()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _initialise_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                f"{_CREATE_TABLE}\n{_CREATE_IDX_TRACE}\n{_CREATE_IDX_EVENT}"
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_event(self, event: ExecutionEvent) -> None:
        """Persist a single :class:`ExecutionEvent` to the database."""
        row = _event_to_row(event)
        with self._lock:
            self._conn.execute(_INSERT, row)
            self._conn.commit()

    def get_trace(self, trace_id: str) -> List[ExecutionEvent]:
        """
        Return all events belonging to *trace_id*, ordered by ``started_at``
        ascending (i.e. chronological order).
        """
        sql = (
            "SELECT * FROM execution_events "
            "WHERE trace_id = ? "
            "ORDER BY started_at ASC;"
        )
        with self._lock:
            rows = self._conn.execute(sql, (trace_id,)).fetchall()
        return [_row_to_event(r) for r in rows]

    def list_traces(self) -> List[Dict[str, Any]]:
        """
        Return a summary list, one entry per distinct trace, containing:

        * ``trace_id``   – the UUID grouping the run
        * ``started_at`` – timestamp of the earliest event in the trace
        * ``total_events`` – number of events recorded
        * ``has_error``  – ``True`` if *any* event in the trace has a non-NULL error
        """
        sql = """
            SELECT
                trace_id,
                MIN(started_at)          AS started_at,
                COUNT(*)                 AS total_events,
                MAX(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS has_error
            FROM execution_events
            GROUP BY trace_id
            ORDER BY started_at ASC;
        """
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            {
                "trace_id": r["trace_id"],
                "started_at": _str_to_dt(r["started_at"]),
                "total_events": r["total_events"],
                "has_error": bool(r["has_error"]),
            }
            for r in rows
        ]

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    # Context-manager support
    def __enter__(self) -> "ExecutionLedger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

