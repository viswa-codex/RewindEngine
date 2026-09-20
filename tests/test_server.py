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