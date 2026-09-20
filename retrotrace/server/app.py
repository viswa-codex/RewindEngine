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


class FlamegraphBar(BaseModel):
    """
    Layout coordinates for a single event's bar in the flamegraph / waterfall view.

    Fields
    ------
    event_id    : links back to EventSchema
    function_name
    module_path
    duration_ms
    has_error
    is_side_effect : True when the function is decorated with @mock_side_effect
    offset_pct  : left edge of the bar as % of total trace duration
                  ``((started_at - min_started_at) / T_total) * 100``
    width_pct   : bar width as % of total trace duration
                  ``max(0.5, duration_ms / T_total_ms * 100)``
    depth       : nesting level — root = 0, direct child = 1, etc.
    """
    event_id: str
    function_name: str
    module_path: str
    duration_ms: float
    has_error: bool
    is_side_effect: bool
    offset_pct: float
    width_pct: float
    depth: int


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
    events: List[EventSchema]       # flat, chronological
    tree: List[TreeNode]            # hierarchical, roots only
    flamegraph: List[FlamegraphBar] # layout-ready bars, ordered by depth then start


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


# ── Tier-2 schemas ─────────────────────────────────────────────────────────

class KeyDiff(BaseModel):
    """Single key-level difference in an inputs/output dict."""
    key: str
    kind: str          # "added" | "removed" | "changed"
    value_a: Optional[Any] = None   # baseline value (None when added)
    value_b: Optional[Any] = None   # candidate value (None when removed)


class DriftDetail(BaseModel):
    """
    One drifted step in the detailed diff.

    drift_kind values
    -----------------
    RETURN_DRIFT    – same inputs, different return values
    EXCEPTION_DRIFT – one side raised an exception, the other did not
                      (or both raised different exceptions)
    ARGUMENT_DRIFT  – same function, inputs diverged (upstream state change)
    TOPOLOGY_DRIFT  – missing or extra function call (unmatched step)
    """
    index: int
    drift_kind: str        # one of the 4 categories above
    function_a: Optional[str] = None
    function_b: Optional[str] = None
    inputs_a: Optional[Dict[str, Any]] = None
    inputs_b: Optional[Dict[str, Any]] = None
    output_a: Optional[Any] = None
    output_b: Optional[Any] = None
    error_a: Optional[str] = None
    error_b: Optional[str] = None
    input_key_diffs: List[KeyDiff] = []
    output_key_diffs: List[KeyDiff] = []


class DriftReport(BaseModel):
    trace_id_1: str
    trace_id_2: str
    total_compared: int
    matching: int
    return_drift: int
    exception_drift: int
    argument_drift: int
    topology_drift: int
    drifts: List[DriftDetail]


class SourceResponse(BaseModel):
    source: str
    file: str
    start_line: int


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


def _build_flamegraph(events: List[ExecutionEvent]) -> List[FlamegraphBar]:
    """
    Compute flamegraph / waterfall layout coordinates for a flat event list.

    Algorithm
    ---------
    1. Determine T_total (max completed_at − min started_at) in milliseconds.
       When a trace has only one event, T_total == event.duration_ms so bars
       fill the full width naturally.
    2. Walk parent → child links with BFS to assign *depth* (root = 0).
    3. For each event, derive:
       - ``offset_pct = (started_at_offset_ms / T_total_ms) * 100``
       - ``width_pct  = max(0.5, duration_ms / T_total_ms * 100)``
         The 0.5 minimum ensures sub-millisecond calls remain visible.
    4. Detect side-effect functions by the ``_retrotrace_side_effect`` attribute
       stamped by ``@mock_side_effect``.  When the decorated object is not
       available at API layer we fall back to ``False``.
    """
    if not events:
        return []

    _SIDE_ATTR = "_retrotrace_side_effect"

    min_start  = min(e.started_at   for e in events)
    max_end    = max(e.completed_at for e in events)
    t_total_ms = (max_end - min_start).total_seconds() * 1000
    if t_total_ms == 0.0:
        # All events are instantaneous — give every bar full width
        t_total_ms = max((e.duration_ms for e in events), default=1.0) or 1.0

    # BFS to assign depth
    by_id: Dict[str, ExecutionEvent] = {e.event_id: e for e in events}
    # child_map: parent_id → [child events]
    children_of: Dict[str, List[str]] = {e.event_id: [] for e in events}
    roots: List[str] = []
    for e in events:
        if e.parent_id and e.parent_id in by_id:
            children_of[e.parent_id].append(e.event_id)
        else:
            roots.append(e.event_id)

    depth_map: Dict[str, int] = {}
    queue: list[tuple[str, int]] = [(eid, 0) for eid in roots]
    while queue:
        eid, d = queue.pop(0)
        depth_map[eid] = d
        for child_id in children_of.get(eid, []):
            queue.append((child_id, d + 1))

    bars: List[FlamegraphBar] = []
    for e in events:
        offset_ms  = (e.started_at - min_start).total_seconds() * 1000
        offset_pct = (offset_ms / t_total_ms) * 100.0
        width_pct  = max(0.5, (e.duration_ms / t_total_ms) * 100.0)

        # Side-effect detection: check module attribute when importable
        is_side_effect = False
        try:
            import importlib
            mod = importlib.import_module(e.module_path)
            fn  = getattr(mod, e.function_name, None)
            if fn is not None:
                is_side_effect = bool(getattr(fn, _SIDE_ATTR, False))
        except Exception:
            pass

        bars.append(FlamegraphBar(
            event_id=e.event_id,
            function_name=e.function_name,
            module_path=e.module_path,
            duration_ms=e.duration_ms,
            has_error=bool(e.error),
            is_side_effect=is_side_effect,
            offset_pct=round(offset_pct, 4),
            width_pct=round(width_pct, 4),
            depth=depth_map.get(e.event_id, 0),
        ))

    # Sort: depth ascending so rows are painted top-to-bottom, tie-break by start
    bars.sort(key=lambda b: (b.depth, b.offset_pct))
    return bars


def _key_diff(a: Optional[Any], b: Optional[Any]) -> List[KeyDiff]:
    """
    Produce a structured list of key-level differences between two JSON-like values.

    Works recursively on dicts; for non-dict primitives/lists it compares the
    whole value and emits a single "changed" entry under the key "__value__".
    """
    result: List[KeyDiff] = []

    if not isinstance(a, dict) or not isinstance(b, dict):
        # Scalar or list-level comparison
        if a != b:
            result.append(KeyDiff(key="__value__", kind="changed", value_a=a, value_b=b))
        return result

    all_keys = set(a) | set(b)
    for k in sorted(all_keys):
        if k not in a:
            result.append(KeyDiff(key=k, kind="added",   value_b=b[k]))
        elif k not in b:
            result.append(KeyDiff(key=k, kind="removed", value_a=a[k]))
        elif a[k] != b[k]:
            result.append(KeyDiff(key=k, kind="changed", value_a=a[k], value_b=b[k]))
    return result


def _classify_drift(
    index: int,
    ea: Optional[Any],   # ExecutionEvent or None
    eb: Optional[Any],   # ExecutionEvent or None
) -> Optional[DriftDetail]:
    """
    Classify a pair of events into one of the 4 drift categories.

    Returns ``None`` when the pair is a clean match (no drift).
    """
    # ── TOPOLOGY_DRIFT: one side is missing ──────────────────────────────────
    if ea is None or eb is None:
        return DriftDetail(
            index=index,
            drift_kind="TOPOLOGY_DRIFT",
            function_a=ea.function_name if ea else None,
            function_b=eb.function_name if eb else None,
            inputs_a=ea.inputs if ea else None,
            inputs_b=eb.inputs if eb else None,
            output_a=ea.output if ea else None,
            output_b=eb.output if eb else None,
            error_a=ea.error if ea else None,
            error_b=eb.error if eb else None,
        )

    # ── TOPOLOGY_DRIFT: different function name ───────────────────────────────
    if ea.function_name != eb.function_name:
        return DriftDetail(
            index=index,
            drift_kind="TOPOLOGY_DRIFT",
            function_a=ea.function_name,
            function_b=eb.function_name,
            inputs_a=ea.inputs, inputs_b=eb.inputs,
            output_a=ea.output, output_b=eb.output,
            error_a=ea.error,   error_b=eb.error,
        )

    common = dict(
        index=index,
        function_a=ea.function_name,
        function_b=eb.function_name,
        inputs_a=ea.inputs, inputs_b=eb.inputs,
        output_a=ea.output, output_b=eb.output,
        error_a=ea.error,   error_b=eb.error,
    )

    # ── EXCEPTION_DRIFT: error presence/content diverged ────────────────────
    if bool(ea.error) != bool(eb.error) or (ea.error and eb.error and ea.error != eb.error):
        return DriftDetail(
            drift_kind="EXCEPTION_DRIFT",
            **common,
        )

    # ── ARGUMENT_DRIFT: inputs diverged ─────────────────────────────────────
    if ea.inputs != eb.inputs:
        return DriftDetail(
            drift_kind="ARGUMENT_DRIFT",
            input_key_diffs=_key_diff(ea.inputs, eb.inputs),
            **common,
        )

    # ── RETURN_DRIFT: same inputs, different output ──────────────────────────
    if ea.output != eb.output:
        return DriftDetail(
            drift_kind="RETURN_DRIFT",
            output_key_diffs=_key_diff(ea.output, eb.output),
            **common,
        )

    return None   # clean match


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
            flamegraph=_build_flamegraph(events),
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

    # ── GET /api/diff/details ──────────────────────────────────────────────
    @app.get("/api/diff/details", response_model=DriftReport, tags=["Diff"])
    def diff_details(
        t1: str = Query(..., description="Baseline trace UUID"),
        t2: str = Query(..., description="Candidate trace UUID"),
    ) -> DriftReport:
        """
        Detailed, categorised drift analysis between two traces.

        Each divergent step is classified as one of:
        ``RETURN_DRIFT`` | ``EXCEPTION_DRIFT`` | ``ARGUMENT_DRIFT`` | ``TOPOLOGY_DRIFT``

        Every drifted step also carries ``input_key_diffs`` / ``output_key_diffs`` —
        fine-grained key-level JSON differences that highlight exactly which fields
        changed, were added, or were removed.
        """
        events_a = ledger.get_trace(t1)
        events_b = ledger.get_trace(t2)
        if not events_a:
            raise HTTPException(status_code=404, detail=f"Trace {t1!r} not found")
        if not events_b:
            raise HTTPException(status_code=404, detail=f"Trace {t2!r} not found")

        max_len = max(len(events_a), len(events_b))
        drifts: List[DriftDetail] = []
        counters = dict(RETURN_DRIFT=0, EXCEPTION_DRIFT=0,
                        ARGUMENT_DRIFT=0, TOPOLOGY_DRIFT=0)

        for i in range(max_len):
            ea = events_a[i] if i < len(events_a) else None
            eb = events_b[i] if i < len(events_b) else None
            detail = _classify_drift(i + 1, ea, eb)
            if detail is not None:
                drifts.append(detail)
                counters[detail.drift_kind] = counters.get(detail.drift_kind, 0) + 1

        return DriftReport(
            trace_id_1=t1, trace_id_2=t2,
            total_compared=max_len,
            matching=max_len - len(drifts),
            return_drift=counters["RETURN_DRIFT"],
            exception_drift=counters["EXCEPTION_DRIFT"],
            argument_drift=counters["ARGUMENT_DRIFT"],
            topology_drift=counters["TOPOLOGY_DRIFT"],
            drifts=drifts,
        )

    # ── GET /api/source ────────────────────────────────────────────────────
    @app.get("/api/source", response_model=SourceResponse, tags=["Source"])
    def get_source(
        module: str = Query(..., description="Dotted module path, e.g. examples.order_service"),
        fn:     str = Query(..., description="Function name, e.g. process_order"),
    ) -> SourceResponse:
        """
        Retrieve the source code of a recorded function using Python's ``inspect``
        module.  The module must be importable from the current Python path.

        Returns 404 when:
        - the module cannot be imported
        - the function does not exist in the module
        - the source is unavailable (built-in, C-extension, etc.)
        """
        import importlib
        import inspect as _inspect

        try:
            mod = importlib.import_module(module)
        except (ImportError, ModuleNotFoundError) as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Module {module!r} could not be imported: {exc}",
            )

        fn_obj = getattr(mod, fn, None)
        if fn_obj is None:
            raise HTTPException(
                status_code=404,
                detail=f"Function {fn!r} not found in module {module!r}",
            )

        # Unwrap decorators to reach the raw function
        unwrapped = _inspect.unwrap(fn_obj)

        try:
            source = _inspect.getsource(unwrapped)
            source_lines, start_line = _inspect.getsourcelines(unwrapped)
            source_file = _inspect.getfile(unwrapped)
        except (OSError, TypeError) as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Source unavailable for {module}.{fn}: {exc}",
            )

        return SourceResponse(
            source=source,
            file=source_file,
            start_line=start_line,
        )

    return app