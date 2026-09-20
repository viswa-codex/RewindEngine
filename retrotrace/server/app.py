"""
retrotrace/server/app.py
========================
FastAPI backend for the RetroTrace Developer Studio.

Endpoints
---------
  GET  /                             → SPA (index.html)
  GET  /api/traces                   → trace summaries
  GET  /api/traces/{trace_id}        → full event list + call tree
  GET  /api/diff?t1={id}&t2={id}     → structured event-by-event diff
  POST /api/replay/{trace_id}        → deterministic API-layer replay

Factory:
    from retrotrace.server.app import create_app
    app = create_app(db_path="my.db")       # accepts str path
    app = create_app(ledger=my_ledger)      # or an existing ledger
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from retrotrace.storage.ledger import ExecutionEvent, ExecutionLedger
from retrotrace.core.replayer import (
    ExecutionDriftError,
    ReplayEngine,
    TraceNotFoundError,
)

_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class TraceSummary(BaseModel):
    trace_id: str
    root_function: str
    started_at: datetime
    total_events: int
    has_error: bool
    duration_ms: float


class EventSchema(BaseModel):
    """Flat representation of a single event (no nested children)."""
    event_id: str
    trace_id: str
    parent_id: Optional[str] = None
    function_name: str
    module_path: str
    inputs: Dict[str, Any]
    output: Optional[Any] = None
    error: Optional[str] = None
    started_at: datetime
    completed_at: datetime
    duration_ms: float


class TreeNode(BaseModel):
    """Hierarchical node for call-tree responses."""
    event_id: str
    function_name: str
    duration_ms: float
    has_error: bool
    children: List["TreeNode"] = []

TreeNode.model_rebuild()


class TraceDetail(BaseModel):
    trace_id: str
    root_function: str
    started_at: datetime
    total_events: int
    has_error: bool
    total_duration_ms: float
    events: List[EventSchema]   # flat, chronological
    tree: List[TreeNode]        # hierarchical, roots only


class ReplayResult(BaseModel):
    trace_id: str
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    drift_detected: bool = False


class DiffEventRow(BaseModel):
    index: int
    status: str   # match | input_drift | output_drift | name_mismatch | missing
    function_a: Optional[str] = None
    function_b: Optional[str] = None
    inputs_a: Optional[Dict[str, Any]] = None
    inputs_b: Optional[Dict[str, Any]] = None
    output_a: Optional[Any] = None
    output_b: Optional[Any] = None
    error_a: Optional[str] = None
    error_b: Optional[str] = None
    duration_ms_a: Optional[float] = None
    duration_ms_b: Optional[float] = None


class DiffResult(BaseModel):
    trace_id_1: str
    trace_id_2: str
    total_events_1: int
    total_events_2: int
    identical: bool
    divergences: int
    events: List[DiffEventRow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_schema(e: ExecutionEvent) -> EventSchema:
    return EventSchema(
        event_id=e.event_id,
        trace_id=e.trace_id,
        parent_id=e.parent_id,
        function_name=e.function_name,
        module_path=e.module_path,
        inputs=e.inputs,
        output=e.output,
        error=e.error,
        started_at=e.started_at,
        completed_at=e.completed_at,
        duration_ms=e.duration_ms,
    )


def _build_tree(events: List[ExecutionEvent]) -> List[TreeNode]:
    nodes: Dict[str, TreeNode] = {
        e.event_id: TreeNode(
            event_id=e.event_id,
            function_name=e.function_name,
            duration_ms=e.duration_ms,
            has_error=bool(e.error),
        )
        for e in events
    }
    roots: List[TreeNode] = []
    for e in events:
        node = nodes[e.event_id]
        if e.parent_id and e.parent_id in nodes:
            nodes[e.parent_id].children.append(node)
        else:
            roots.append(node)

    def _sort(n: TreeNode) -> None:
        n.children.sort(key=lambda c: c.function_name)
        for c in n.children:
            _sort(c)

    roots.sort(key=lambda n: n.function_name)
    for r in roots:
        _sort(r)
    return roots


def _total_duration(events: List[ExecutionEvent]) -> float:
    if not events:
        return 0.0
    return (max(e.completed_at for e in events) -
            min(e.started_at for e in events)).total_seconds() * 1000


def _root_fn(events: List[ExecutionEvent]) -> str:
    return events[0].function_name if events else "—"


def _api_replay(
    engine: ReplayEngine,
    trace_id: str,
    root_event: ExecutionEvent,
) -> Any:
    events = engine._ledger.get_trace(trace_id)
    if not events:
        raise TraceNotFoundError(f"No events for trace {trace_id!r}")
    for event in events:
        if event.error:
            last_line = event.error.strip().splitlines()[-1]
            raise RuntimeError(
                f"Replaying recorded error in {event.function_name!r}: {last_line}"
            )
    return root_event.output


# ---------------------------------------------------------------------------
# App factory — accepts either a db_path str OR a pre-built ledger
# ---------------------------------------------------------------------------

def create_app(
    db_path: str | None = None,
    *,
    ledger: ExecutionLedger | None = None,
) -> FastAPI:
    """
    Build the RetroTrace FastAPI application.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. Mutually exclusive with *ledger*.
    ledger:
        Pre-built ``ExecutionLedger`` instance (used by tests to inject a
        temporary ledger without touching the filesystem path).
    """
    if ledger is None and db_path is None:
        raise ValueError("Provide either db_path or ledger")
    if ledger is None:
        ledger = ExecutionLedger(db_path=db_path)  # type: ignore[arg-type]

    app = FastAPI(
        title="RetroTrace Developer Studio",
        description="Execution tracing, replay, and diff REST API.",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    engine = ReplayEngine(ledger=ledger)

    # Static files
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ── GET / ──────────────────────────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    def serve_index():
        index_path = _STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(str(index_path), media_type="text/html")
        return {"message": "RetroTrace Studio — index.html not found"}

    # ── GET /api/traces ────────────────────────────────────────────────────
    @app.get("/api/traces", response_model=List[TraceSummary], tags=["Traces"])
    def list_traces() -> List[TraceSummary]:
        summaries = ledger.list_traces()
        result: List[TraceSummary] = []
        for s in summaries:
            evs = ledger.get_trace(s["trace_id"])
            result.append(TraceSummary(
                trace_id=s["trace_id"],
                root_function=_root_fn(evs),
                started_at=s["started_at"],
                total_events=s["total_events"],
                has_error=s["has_error"],
                duration_ms=_total_duration(evs),
            ))
        result.sort(key=lambda t: t.started_at, reverse=True)
        return result

    # ── GET /api/traces/{trace_id} ─────────────────────────────────────────
    @app.get("/api/traces/{trace_id}", response_model=TraceDetail, tags=["Traces"])
    def get_trace(trace_id: str) -> TraceDetail:
        events = ledger.get_trace(trace_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"Trace {trace_id!r} not found")
        return TraceDetail(
            trace_id=trace_id,
            root_function=_root_fn(events),
            started_at=events[0].started_at,
            total_events=len(events),
            has_error=any(e.error for e in events),
            total_duration_ms=_total_duration(events),
            events=[_to_schema(e) for e in events],
            tree=_build_tree(events),
        )

    # ── POST /api/replay/{trace_id} ────────────────────────────────────────
    @app.post("/api/replay/{trace_id}", response_model=ReplayResult, tags=["Replay"])
    def replay_trace(trace_id: str) -> ReplayResult:
        events = ledger.get_trace(trace_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"Trace {trace_id!r} not found")
        try:
            result = _api_replay(engine, trace_id, events[0])
            return ReplayResult(trace_id=trace_id, success=True, result=result)
        except ExecutionDriftError as exc:
            return ReplayResult(trace_id=trace_id, success=False,
                                error=str(exc), drift_detected=True)
        except TraceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:
            return ReplayResult(trace_id=trace_id, success=False, error=str(exc))

    # ── GET /api/diff ──────────────────────────────────────────────────────
    @app.get("/api/diff", response_model=DiffResult, tags=["Diff"])
    def diff_traces(
        t1: str = Query(..., description="First trace UUID"),
        t2: str = Query(..., description="Second trace UUID"),
    ) -> DiffResult:
        events_a = ledger.get_trace(t1)
        events_b = ledger.get_trace(t2)
        if not events_a:
            raise HTTPException(status_code=404, detail=f"Trace {t1!r} not found")
        if not events_b:
            raise HTTPException(status_code=404, detail=f"Trace {t2!r} not found")

        max_len = max(len(events_a), len(events_b))
        rows: List[DiffEventRow] = []
        divergences = 0

        for i in range(max_len):
            ea = events_a[i] if i < len(events_a) else None
            eb = events_b[i] if i < len(events_b) else None

            if ea is None or eb is None:
                status = "missing"; divergences += 1
            elif ea.function_name != eb.function_name:
                status = "name_mismatch"; divergences += 1
            elif ea.inputs != eb.inputs:
                status = "input_drift"; divergences += 1
            elif ea.output != eb.output or ea.error != eb.error:
                status = "output_drift"; divergences += 1
            else:
                status = "match"

            rows.append(DiffEventRow(
                index=i + 1, status=status,
                function_a=ea.function_name if ea else None,
                function_b=eb.function_name if eb else None,
                inputs_a=ea.inputs if ea else None,
                inputs_b=eb.inputs if eb else None,
                output_a=ea.output if ea else None,
                output_b=eb.output if eb else None,
                error_a=ea.error if ea else None,
                error_b=eb.error if eb else None,
                duration_ms_a=ea.duration_ms if ea else None,
                duration_ms_b=eb.duration_ms if eb else None,
            ))

        return DiffResult(
            trace_id_1=t1, trace_id_2=t2,
            total_events_1=len(events_a), total_events_2=len(events_b),
            identical=(divergences == 0), divergences=divergences,
            events=rows,
        )

    return app