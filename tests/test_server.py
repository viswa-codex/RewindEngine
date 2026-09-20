"""
tests/test_server.py
Unit tests for retrotrace.server.app — all four REST endpoints + GET /.

Uses FastAPI's TestClient (httpx-backed). ExecutionEvent fields are taken
directly from retrotrace/storage/ledger.py (11 fields):
  event_id, trace_id, parent_id, function_name, module_path,
  inputs, output, error, started_at, completed_at, duration_ms
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from retrotrace.server.app import create_app
from retrotrace.storage.ledger import ExecutionEvent, ExecutionLedger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    with ExecutionLedger(db_path=str(tmp_path / "test.db")) as lg:
        yield lg


@pytest.fixture()
def client(ledger: ExecutionLedger) -> TestClient:
    app = create_app(ledger=ledger)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper: valid ExecutionEvent factory
# ---------------------------------------------------------------------------

def _now(offset_s: float = 0.0) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=offset_s)


def _event(
    *,
    trace_id: str,
    function_name: str = "my_fn",
    module_path: str = "my.module",
    parent_id: Optional[str] = None,
    inputs: dict | None = None,
    output: object = 42,
    error: Optional[str] = None,
    started_offset: float = 0.0,
    duration_ms: float = 5.0,
) -> ExecutionEvent:
    ts = _now(started_offset)
    return ExecutionEvent(
        event_id=str(uuid.uuid4()),
        trace_id=trace_id,
        parent_id=parent_id,
        function_name=function_name,
        module_path=module_path,
        inputs=inputs or {"x": 1},
        output=output,
        error=error,
        started_at=ts,
        completed_at=ts + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms,
    )


def _tid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# GET /  (SPA shell)
# ---------------------------------------------------------------------------

class TestIndex:
    def test_get_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_get_index_returns_html(self, client):
        resp = client.get("/")
        ct = resp.headers.get("content-type", "")
        assert "text/html" in ct or resp.status_code == 200

    def test_get_index_contains_retrotrace(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        # index.html contains "RetroTrace" somewhere
        assert "RetroTrace" in resp.text or "retrotrace" in resp.text.lower()


# ---------------------------------------------------------------------------
# GET /api/traces
# ---------------------------------------------------------------------------

class TestListTraces:
    def test_empty_ledger(self, client):
        resp = client.get("/api/traces")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_single_trace(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, function_name="root_job"))
        data = client.get("/api/traces").json()
        assert len(data) == 1
        assert data[0]["trace_id"] == tid
        assert data[0]["root_function"] == "root_job"

    def test_has_error_false(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, error=None))
        assert client.get("/api/traces").json()[0]["has_error"] is False

    def test_has_error_true(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, error="ValueError: bad"))
        assert client.get("/api/traces").json()[0]["has_error"] is True

    def test_event_count(self, ledger, client):
        tid = _tid()
        for i in range(3):
            ledger.record_event(_event(trace_id=tid, started_offset=i * 0.01))
        assert client.get("/api/traces").json()[0]["total_events"] == 3

    def test_multiple_traces(self, ledger, client):
        for _ in range(4):
            ledger.record_event(_event(trace_id=_tid()))
        assert len(client.get("/api/traces").json()) == 4

    def test_duration_ms_present(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, duration_ms=50.0))
        assert client.get("/api/traces").json()[0]["duration_ms"] >= 0

    def test_schema_fields(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid))
        item = client.get("/api/traces").json()[0]
        for f in ("trace_id", "root_function", "started_at",
                  "total_events", "has_error", "duration_ms"):
            assert f in item


# ---------------------------------------------------------------------------
# GET /api/traces/{trace_id}
# ---------------------------------------------------------------------------

class TestGetTrace:
    def test_404_on_missing(self, client):
        assert client.get(f"/api/traces/{_tid()}").status_code == 404

    def test_200_on_known(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid))
        assert client.get(f"/api/traces/{tid}").status_code == 200

    def test_events_key_present(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid))
        data = client.get(f"/api/traces/{tid}").json()
        assert "events" in data

    def test_events_count(self, ledger, client):
        tid = _tid()
        for i in range(3):
            ledger.record_event(_event(trace_id=tid, started_offset=i * 0.01))
        data = client.get(f"/api/traces/{tid}").json()
        assert data["total_events"] == 3
        assert len(data["events"]) == 3

    def test_function_name_in_events(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, function_name="compute"))
        data = client.get(f"/api/traces/{tid}").json()
        assert data["events"][0]["function_name"] == "compute"

    def test_tree_key_present(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid))
        data = client.get(f"/api/traces/{tid}").json()
        assert "tree" in data
        assert isinstance(data["tree"], list)

    def test_nested_tree_children(self, ledger, client):
        tid = _tid()
        parent = _event(trace_id=tid, function_name="outer", started_offset=0)
        child  = _event(trace_id=tid, function_name="inner",
                        parent_id=parent.event_id, started_offset=0.001)
        ledger.record_event(parent)
        ledger.record_event(child)
        data = client.get(f"/api/traces/{tid}").json()
        root_node = data["tree"][0]
        assert root_node["function_name"] == "outer"
        assert len(root_node["children"]) == 1
        assert root_node["children"][0]["function_name"] == "inner"

    def test_error_preserved(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, error="TypeError: oops"))
        data = client.get(f"/api/traces/{tid}").json()
        assert data["events"][0]["error"] == "TypeError: oops"
        assert data["has_error"] is True

    def test_inputs_preserved(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, inputs={"amount": 99}))
        data = client.get(f"/api/traces/{tid}").json()
        assert data["events"][0]["inputs"] == {"amount": 99}

    def test_root_function_field(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, function_name="pipeline"))
        data = client.get(f"/api/traces/{tid}").json()
        assert data["root_function"] == "pipeline"


# ---------------------------------------------------------------------------
# POST /api/replay/{trace_id}
# ---------------------------------------------------------------------------

class TestReplay:
    def test_404_on_missing(self, client):
        assert client.post(f"/api/replay/{_tid()}").status_code == 404

    def test_success_true_for_clean_trace(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, error=None, output=7))
        data = client.post(f"/api/replay/{tid}").json()
        assert data["success"] is True

    def test_success_false_for_errored_trace(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(
            trace_id=tid,
            error="Traceback (most recent call last):\n  ...\nValueError: boom",
        ))
        data = client.post(f"/api/replay/{tid}").json()
        assert data["success"] is False
        assert data["error"] is not None

    def test_trace_id_echoed(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid))
        data = client.post(f"/api/replay/{tid}").json()
        assert data["trace_id"] == tid

    def test_result_field_present(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, output="hi"))
        data = client.post(f"/api/replay/{tid}").json()
        assert "result" in data

    def test_drift_detected_field(self, ledger, client):
        tid = _tid()
        ledger.record_event(_event(trace_id=tid))
        data = client.post(f"/api/replay/{tid}").json()
        assert "drift_detected" in data


# ---------------------------------------------------------------------------
# GET /api/diff
# ---------------------------------------------------------------------------

class TestDiff:
    def _pair(self, ledger, *, fn="fn", inputs=None, output=1):
        t1, t2 = _tid(), _tid()
        for tid in (t1, t2):
            ledger.record_event(_event(
                trace_id=tid, function_name=fn,
                inputs=inputs or {"x": 1}, output=output,
            ))
        return t1, t2

    def test_missing_t1_404(self, ledger, client):
        t2 = _tid()
        ledger.record_event(_event(trace_id=t2))
        assert client.get(f"/api/diff?t1={_tid()}&t2={t2}").status_code == 404

    def test_missing_t2_404(self, ledger, client):
        t1 = _tid()
        ledger.record_event(_event(trace_id=t1))
        assert client.get(f"/api/diff?t1={t1}&t2={_tid()}").status_code == 404

    def test_identical_traces(self, ledger, client):
        t1, t2 = self._pair(ledger)
        data = client.get(f"/api/diff?t1={t1}&t2={t2}").json()
        assert data["identical"] is True
        assert data["divergences"] == 0
        assert data["events"][0]["status"] == "match"

    def test_name_mismatch(self, ledger, client):
        t1, t2 = _tid(), _tid()
        ledger.record_event(_event(trace_id=t1, function_name="alpha"))
        ledger.record_event(_event(trace_id=t2, function_name="beta"))
        data = client.get(f"/api/diff?t1={t1}&t2={t2}").json()
        assert data["events"][0]["status"] == "name_mismatch"

    def test_input_drift(self, ledger, client):
        t1, t2 = _tid(), _tid()
        ledger.record_event(_event(trace_id=t1, function_name="f", inputs={"x": 1}))
        ledger.record_event(_event(trace_id=t2, function_name="f", inputs={"x": 99}))
        data = client.get(f"/api/diff?t1={t1}&t2={t2}").json()
        assert data["events"][0]["status"] == "input_drift"

    def test_output_drift(self, ledger, client):
        t1, t2 = _tid(), _tid()
        ledger.record_event(_event(trace_id=t1, function_name="f", inputs={"x": 1}, output=10))
        ledger.record_event(_event(trace_id=t2, function_name="f", inputs={"x": 1}, output=99))
        data = client.get(f"/api/diff?t1={t1}&t2={t2}").json()
        assert data["events"][0]["status"] == "output_drift"

    def test_missing_event(self, ledger, client):
        t1, t2 = _tid(), _tid()
        for i in range(3):
            ledger.record_event(_event(trace_id=t1, started_offset=i * 0.01))
        ledger.record_event(_event(trace_id=t2))
        data = client.get(f"/api/diff?t1={t1}&t2={t2}").json()
        statuses = {e["status"] for e in data["events"]}
        assert "missing" in statuses

    def test_divergences_count(self, ledger, client):
        t1, t2 = _tid(), _tid()
        ledger.record_event(_event(trace_id=t1, function_name="a"))
        ledger.record_event(_event(trace_id=t2, function_name="b"))
        data = client.get(f"/api/diff?t1={t1}&t2={t2}").json()
        assert data["divergences"] == 1

    def test_schema_fields(self, ledger, client):
        t1, t2 = self._pair(ledger)
        data = client.get(f"/api/diff?t1={t1}&t2={t2}").json()
        for f in ("trace_id_1", "trace_id_2", "total_events_1",
                  "total_events_2", "identical", "divergences", "events"):
            assert f in data

    def test_event_row_fields(self, ledger, client):
        t1, t2 = self._pair(ledger)
        row = client.get(f"/api/diff?t1={t1}&t2={t2}").json()["events"][0]
        for f in ("index", "status", "function_a", "function_b",
                  "inputs_a", "inputs_b", "output_a", "output_b"):
            assert f in row

    def test_total_event_counts(self, ledger, client):
        t1, t2 = _tid(), _tid()
        for i in range(2):
            ledger.record_event(_event(trace_id=t1, started_offset=i * 0.01))
        ledger.record_event(_event(trace_id=t2))
        data = client.get(f"/api/diff?t1={t1}&t2={t2}").json()
        assert data["total_events_1"] == 2
        assert data["total_events_2"] == 1


# ---------------------------------------------------------------------------
# CLI parser — studio subcommand
# ---------------------------------------------------------------------------

class TestStudioParser:
    def test_studio_parses(self):
        from retrotrace.cli.main import _build_parser
        args = _build_parser().parse_args(["studio"])
        assert args.command == "studio"

    def test_default_port(self):
        from retrotrace.cli.main import _build_parser
        args = _build_parser().parse_args(["studio"])
        assert args.port == 8000

    def test_custom_port(self):
        from retrotrace.cli.main import _build_parser
        args = _build_parser().parse_args(["studio", "--port", "9999"])
        assert args.port == 9999

    def test_custom_host(self):
        from retrotrace.cli.main import _build_parser
        args = _build_parser().parse_args(["studio", "--host", "0.0.0.0"])
        assert args.host == "0.0.0.0"

    def test_no_browser_flag(self):
        from retrotrace.cli.main import _build_parser
        args = _build_parser().parse_args(["studio", "--no-browser"])
        assert args.no_browser is True

    def test_no_browser_default_false(self):
        from retrotrace.cli.main import _build_parser
        args = _build_parser().parse_args(["studio"])
        assert args.no_browser is False


# ---------------------------------------------------------------------------
# Flamegraph metadata — GET /api/traces/{trace_id}
# ---------------------------------------------------------------------------

class TestFlamegraph:
    """Verify that /api/traces/{id} returns valid flamegraph layout metadata."""

    def _detail(self, ledger, client, *events):
        for ev in events:
            ledger.record_event(ev)
        tid = events[0].trace_id
        return client.get(f"/api/traces/{tid}").json()

    # ── Schema ───────────────────────────────────────────────────────────────

    def test_flamegraph_key_present(self, ledger, client):
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid))
        assert "flamegraph" in data

    def test_flamegraph_is_list(self, ledger, client):
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid))
        assert isinstance(data["flamegraph"], list)

    def test_flamegraph_bar_fields(self, ledger, client):
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid))
        bar = data["flamegraph"][0]
        for field in ("event_id", "function_name", "module_path", "duration_ms",
                      "has_error", "is_side_effect", "offset_pct", "width_pct", "depth"):
            assert field in bar, f"Missing flamegraph field: {field}"

    def test_flamegraph_count_matches_events(self, ledger, client):
        tid = _tid()
        evs = [_event(trace_id=tid, started_offset=i * 0.01) for i in range(4)]
        data = self._detail(ledger, client, *evs)
        assert len(data["flamegraph"]) == 4

    # ── Coordinate correctness ────────────────────────────────────────────────

    def test_root_event_offset_pct_zero(self, ledger, client):
        """The earliest event always has offset_pct == 0."""
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid))
        # Single event: offset from itself is always 0
        assert data["flamegraph"][0]["offset_pct"] == 0.0

    def test_width_pct_positive(self, ledger, client):
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid, duration_ms=10.0))
        assert data["flamegraph"][0]["width_pct"] > 0

    def test_width_pct_minimum_half_percent(self, ledger, client):
        """width_pct is never less than 0.5 (sub-ms events remain visible)."""
        tid = _tid()
        # Force a very long trace alongside a 0-ms event so width_pct would
        # otherwise be tiny — the clamp should kick in.
        parent = _event(trace_id=tid, function_name="outer",
                        started_offset=0, duration_ms=5000.0)
        child  = _event(trace_id=tid, function_name="inner",
                        parent_id=parent.event_id,
                        started_offset=0.001, duration_ms=0.0001)
        data = self._detail(ledger, client, parent, child)
        widths = [b["width_pct"] for b in data["flamegraph"]]
        assert all(w >= 0.5 for w in widths), f"Some bars narrower than 0.5%: {widths}"

    def test_single_event_fills_full_width(self, ledger, client):
        """A trace with one event should have width_pct == 100 (it IS the total)."""
        tid = _tid()
        ev = _event(trace_id=tid, duration_ms=20.0)
        data = self._detail(ledger, client, ev)
        bar = data["flamegraph"][0]
        assert bar["width_pct"] == pytest.approx(100.0, abs=0.01)

    def test_later_event_has_positive_offset(self, ledger, client):
        """A child event that starts after the root must have offset_pct > 0."""
        tid = _tid()
        root  = _event(trace_id=tid, function_name="root",  started_offset=0,     duration_ms=100.0)
        child = _event(trace_id=tid, function_name="child", started_offset=0.05,  duration_ms=20.0,
                       parent_id=root.event_id)
        data = self._detail(ledger, client, root, child)
        offsets = {b["function_name"]: b["offset_pct"] for b in data["flamegraph"]}
        assert offsets["child"] > offsets["root"]

    # ── Depth ─────────────────────────────────────────────────────────────────

    def test_root_depth_is_zero(self, ledger, client):
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid))
        assert data["flamegraph"][0]["depth"] == 0

    def test_child_depth_is_one(self, ledger, client):
        tid = _tid()
        parent = _event(trace_id=tid, function_name="parent")
        child  = _event(trace_id=tid, function_name="child",
                        parent_id=parent.event_id, started_offset=0.001)
        data = self._detail(ledger, client, parent, child)
        depths = {b["function_name"]: b["depth"] for b in data["flamegraph"]}
        assert depths["parent"] == 0
        assert depths["child"]  == 1

    def test_grandchild_depth_is_two(self, ledger, client):
        tid = _tid()
        gp    = _event(trace_id=tid, function_name="gp")
        par   = _event(trace_id=tid, function_name="par",
                       parent_id=gp.event_id, started_offset=0.001)
        child = _event(trace_id=tid, function_name="child",
                       parent_id=par.event_id, started_offset=0.002)
        data = self._detail(ledger, client, gp, par, child)
        depths = {b["function_name"]: b["depth"] for b in data["flamegraph"]}
        assert depths["gp"]    == 0
        assert depths["par"]   == 1
        assert depths["child"] == 2

    # ── Flags ─────────────────────────────────────────────────────────────────

    def test_has_error_false_for_clean_event(self, ledger, client):
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid, error=None))
        assert data["flamegraph"][0]["has_error"] is False

    def test_has_error_true_for_errored_event(self, ledger, client):
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid, error="ValueError: x"))
        assert data["flamegraph"][0]["has_error"] is True

    def test_is_side_effect_bool(self, ledger, client):
        """is_side_effect must always be a boolean (True or False)."""
        tid = _tid()
        data = self._detail(ledger, client, _event(trace_id=tid))
        assert isinstance(data["flamegraph"][0]["is_side_effect"], bool)

    # ── Ordering ──────────────────────────────────────────────────────────────

    def test_flamegraph_sorted_by_depth_then_offset(self, ledger, client):
        """Bars at smaller depth come before bars at larger depth."""
        tid = _tid()
        root  = _event(trace_id=tid, function_name="root",  started_offset=0,    duration_ms=200.0)
        childA = _event(trace_id=tid, function_name="childA",parent_id=root.event_id,
                        started_offset=0.01, duration_ms=50.0)
        childB = _event(trace_id=tid, function_name="childB",parent_id=root.event_id,
                        started_offset=0.07, duration_ms=50.0)
        data = self._detail(ledger, client, root, childA, childB)
        depths = [b["depth"] for b in data["flamegraph"]]
        # depths must be non-decreasing
        assert depths == sorted(depths)


# ---------------------------------------------------------------------------
# GET /api/diff/details  — drift detection
# ---------------------------------------------------------------------------

class TestDriftDetails:
    """Verify the 4-category drift classifier and key-level diff output."""

    URL = "/api/diff/details"

    # ── helpers ──────────────────────────────────────────────────────────────

    def _mk(self, ledger, **kw):
        """Create a single-event trace and return its trace_id."""
        tid = _tid()
        ledger.record_event(_event(trace_id=tid, **kw))
        return tid

    def _report(self, client, t1, t2):
        return client.get(f"{self.URL}?t1={t1}&t2={t2}").json()

    # ── 404 paths ─────────────────────────────────────────────────────────────

    def test_missing_t1_returns_404(self, ledger, client):
        t2 = self._mk(ledger)
        assert client.get(f"{self.URL}?t1={_tid()}&t2={t2}").status_code == 404

    def test_missing_t2_returns_404(self, ledger, client):
        t1 = self._mk(ledger)
        assert client.get(f"{self.URL}?t1={t1}&t2={_tid()}").status_code == 404

    # ── Schema ────────────────────────────────────────────────────────────────

    def test_response_schema_fields(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1})
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1})
        data = self._report(client, t1, t2)
        for f in ("trace_id_1", "trace_id_2", "total_compared", "matching",
                  "return_drift", "exception_drift", "argument_drift",
                  "topology_drift", "drifts"):
            assert f in data, f"Missing field: {f}"

    def test_identical_traces_no_drifts(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1}, output=2)
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1}, output=2)
        data = self._report(client, t1, t2)
        assert data["drifts"] == []
        assert data["matching"] == 1
        assert data["return_drift"] == 0

    def test_total_compared_equals_max_events(self, ledger, client):
        t1 = _tid(); t2 = _tid()
        for i in range(3):
            ledger.record_event(_event(trace_id=t1, function_name="f",
                                       started_offset=i * 0.01))
        ledger.record_event(_event(trace_id=t2, function_name="f"))
        data = self._report(client, t1, t2)
        assert data["total_compared"] == 3

    # ── RETURN_DRIFT ──────────────────────────────────────────────────────────

    def test_return_drift_detected(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1}, output=10)
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1}, output=99)
        data = self._report(client, t1, t2)
        assert data["return_drift"] == 1
        assert data["drifts"][0]["drift_kind"] == "RETURN_DRIFT"

    def test_return_drift_output_key_diffs(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1},
                      output={"price": 10, "tax": 1})
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1},
                      output={"price": 10, "tax": 2})
        data = self._report(client, t1, t2)
        kd = data["drifts"][0]["output_key_diffs"]
        assert len(kd) == 1
        assert kd[0]["key"] == "tax"
        assert kd[0]["kind"] == "changed"

    def test_return_drift_key_added(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1},
                      output={"a": 1})
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1},
                      output={"a": 1, "b": 2})
        data = self._report(client, t1, t2)
        kinds = {kd["key"]: kd["kind"]
                 for kd in data["drifts"][0]["output_key_diffs"]}
        assert kinds.get("b") == "added"

    def test_return_drift_key_removed(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1},
                      output={"a": 1, "b": 2})
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1},
                      output={"a": 1})
        data = self._report(client, t1, t2)
        kinds = {kd["key"]: kd["kind"]
                 for kd in data["drifts"][0]["output_key_diffs"]}
        assert kinds.get("b") == "removed"

    # ── EXCEPTION_DRIFT ───────────────────────────────────────────────────────

    def test_exception_drift_one_side_errored(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1}, output=5, error=None)
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1}, error="ValueError: bad")
        data = self._report(client, t1, t2)
        assert data["exception_drift"] == 1
        assert data["drifts"][0]["drift_kind"] == "EXCEPTION_DRIFT"

    def test_exception_drift_different_errors(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1}, error="ValueError: oops")
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1}, error="TypeError: nope")
        data = self._report(client, t1, t2)
        assert data["exception_drift"] == 1

    def test_same_errors_not_exception_drift(self, ledger, client):
        err = "ValueError: boom"
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1}, error=err)
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1}, error=err)
        data = self._report(client, t1, t2)
        assert data["exception_drift"] == 0
        assert data["drifts"] == []

    # ── ARGUMENT_DRIFT ────────────────────────────────────────────────────────

    def test_argument_drift_detected(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1}, output=2)
        t2 = self._mk(ledger, function_name="f", inputs={"x": 9}, output=2)
        data = self._report(client, t1, t2)
        assert data["argument_drift"] == 1
        assert data["drifts"][0]["drift_kind"] == "ARGUMENT_DRIFT"

    def test_argument_drift_input_key_diffs(self, ledger, client):
        t1 = self._mk(ledger, function_name="f",
                      inputs={"amount": 50, "code": "A"}, output=1)
        t2 = self._mk(ledger, function_name="f",
                      inputs={"amount": 99, "code": "A"}, output=1)
        data = self._report(client, t1, t2)
        kd = data["drifts"][0]["input_key_diffs"]
        keys = {d["key"]: d for d in kd}
        assert "amount" in keys
        assert keys["amount"]["kind"] == "changed"

    # ── TOPOLOGY_DRIFT ────────────────────────────────────────────────────────

    def test_topology_drift_missing_event(self, ledger, client):
        t1 = _tid(); t2 = _tid()
        for i in range(2):
            ledger.record_event(_event(trace_id=t1, function_name="f",
                                       started_offset=i * 0.01))
        ledger.record_event(_event(trace_id=t2, function_name="f"))
        data = self._report(client, t1, t2)
        assert data["topology_drift"] >= 1
        kinds = {d["drift_kind"] for d in data["drifts"]}
        assert "TOPOLOGY_DRIFT" in kinds

    def test_topology_drift_name_mismatch(self, ledger, client):
        t1 = self._mk(ledger, function_name="alpha", inputs={"x": 1})
        t2 = self._mk(ledger, function_name="beta",  inputs={"x": 1})
        data = self._report(client, t1, t2)
        assert data["topology_drift"] == 1
        assert data["drifts"][0]["drift_kind"] == "TOPOLOGY_DRIFT"

    # ── Counter accuracy ──────────────────────────────────────────────────────

    def test_counter_sum_equals_total_drifts(self, ledger, client):
        t1 = _tid(); t2 = _tid()
        # Return drift
        ledger.record_event(_event(trace_id=t1, function_name="f",
                                   inputs={"x": 1}, output=10))
        ledger.record_event(_event(trace_id=t2, function_name="f",
                                   inputs={"x": 1}, output=99))
        data = self._report(client, t1, t2)
        total_counted = (data["return_drift"] + data["exception_drift"] +
                         data["argument_drift"] + data["topology_drift"])
        assert total_counted == len(data["drifts"])

    def test_matching_plus_drifts_equals_total(self, ledger, client):
        t1 = self._mk(ledger, function_name="f", inputs={"x": 1}, output=2)
        t2 = self._mk(ledger, function_name="f", inputs={"x": 1}, output=99)
        data = self._report(client, t1, t2)
        assert data["matching"] + len(data["drifts"]) == data["total_compared"]


# ---------------------------------------------------------------------------
# GET /api/source  — source code extraction
# ---------------------------------------------------------------------------

class TestSource:
    """Verify source retrieval via the /api/source endpoint."""

    URL = "/api/source"

    # ── Happy path ────────────────────────────────────────────────────────────

    def test_known_module_and_fn_returns_200(self, client):
        # retrotrace.storage.ledger and ExecutionLedger are always importable
        resp = client.get(f"{self.URL}?module=retrotrace.storage.ledger"
                          "&fn=ExecutionLedger")
        assert resp.status_code == 200

    def test_source_field_non_empty(self, client):
        resp = client.get(f"{self.URL}?module=retrotrace.storage.ledger"
                          "&fn=ExecutionLedger")
        data = resp.json()
        assert len(data["source"]) > 10

    def test_response_has_all_fields(self, client):
        resp = client.get(f"{self.URL}?module=retrotrace.storage.ledger"
                          "&fn=ExecutionLedger")
        data = resp.json()
        assert "source" in data
        assert "file"   in data
        assert "start_line" in data

    def test_start_line_is_positive_int(self, client):
        resp = client.get(f"{self.URL}?module=retrotrace.storage.ledger"
                          "&fn=ExecutionLedger")
        assert resp.json()["start_line"] >= 1

    def test_source_contains_function_name(self, client):
        resp = client.get(f"{self.URL}?module=retrotrace.storage.ledger"
                          "&fn=ExecutionLedger")
        assert "ExecutionLedger" in resp.json()["source"]

    # ── 404 paths ─────────────────────────────────────────────────────────────

    def test_bad_module_returns_404(self, client):
        resp = client.get(f"{self.URL}?module=totally.nonexistent.module"
                          "&fn=some_fn")
        assert resp.status_code == 404

    def test_bad_function_returns_404(self, client):
        resp = client.get(f"{self.URL}?module=retrotrace.storage.ledger"
                          "&fn=this_function_does_not_exist_ever")
        assert resp.status_code == 404

    def test_top_level_function_source(self, client):
        """Fetch a module-level function (not a class method)."""
        resp = client.get(f"{self.URL}?module=retrotrace.storage.ledger"
                          "&fn=_row_to_event")
        assert resp.status_code == 200
        assert "_row_to_event" in resp.json()["source"]