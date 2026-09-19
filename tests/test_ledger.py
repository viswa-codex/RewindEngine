"""
tests/test_ledger.py
Unit tests for retrotrace.storage.ledger — ExecutionLedger & ExecutionEvent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from retrotrace.storage.ledger import ExecutionEvent, ExecutionLedger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_event(
    *,
    trace_id: str | None = None,
    parent_id: str | None = None,
    function_name: str = "my_func",
    module_path: str = "myapp.module",
    inputs: dict | None = None,
    output=None,
    error: str | None = None,
    duration_ms: float = 12.5,
) -> ExecutionEvent:
    """Factory that fills in sensible defaults for every test."""
    now = datetime.now(tz=timezone.utc)
    return ExecutionEvent(
        event_id=str(uuid.uuid4()),
        trace_id=trace_id or str(uuid.uuid4()),
        parent_id=parent_id,
        function_name=function_name,
        module_path=module_path,
        inputs=inputs if inputs is not None else {"x": 1, "y": "hello"},
        output=output,
        error=error,
        started_at=now,
        completed_at=now,
        duration_ms=duration_ms,
    )


@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    """Fresh in-memory-ish ledger backed by a temp directory."""
    db_file = tmp_path / "test_retrotrace.db"
    with ExecutionLedger(db_path=db_file) as ledger:
        yield ledger


# ---------------------------------------------------------------------------
# ExecutionEvent model tests
# ---------------------------------------------------------------------------

class TestExecutionEventModel:
    def test_valid_construction(self):
        event = _make_event()
        assert isinstance(event.event_id, str)
        assert event.parent_id is None
        assert event.duration_ms == 12.5

    def test_frozen_model_raises_on_mutation(self):
        event = _make_event()
        with pytest.raises(Exception):
            # frozen=True means attribute assignment must raise
            object.__setattr__(event, "function_name", "other")  # noqa: PT011
            # Pydantic v2 frozen models raise ValidationError on __setattr__
            event.function_name = "other"

    def test_optional_fields_default_none(self):
        event = _make_event()
        assert event.parent_id is None
        assert event.output is None
        assert event.error is None

    def test_complex_inputs_and_output(self):
        event = _make_event(
            inputs={"nested": {"a": [1, 2, 3]}, "flag": True},
            output={"result": 42},
        )
        assert event.inputs["nested"]["a"] == [1, 2, 3]
        assert event.output["result"] == 42


# ---------------------------------------------------------------------------
# Insertion tests
# ---------------------------------------------------------------------------

class TestRecordEvent:
    def test_single_insert_roundtrip(self, ledger: ExecutionLedger):
        event = _make_event()
        ledger.record_event(event)
        retrieved = ledger.get_trace(event.trace_id)
        assert len(retrieved) == 1
        assert retrieved[0].event_id == event.event_id

    def test_fields_survive_roundtrip(self, ledger: ExecutionLedger):
        event = _make_event(
            function_name="compute",
            module_path="pkg.sub.mod",
            inputs={"val": [1, 2, 3]},
            output={"sum": 6},
            error=None,
            duration_ms=99.9,
        )
        ledger.record_event(event)
        back = ledger.get_trace(event.trace_id)[0]

        assert back.function_name == "compute"
        assert back.module_path == "pkg.sub.mod"
        assert back.inputs == {"val": [1, 2, 3]}
        assert back.output == {"sum": 6}
        assert back.error is None
        assert abs(back.duration_ms - 99.9) < 1e-6

    def test_error_field_persisted(self, ledger: ExecutionLedger):
        event = _make_event(error="ZeroDivisionError: division by zero")
        ledger.record_event(event)
        back = ledger.get_trace(event.trace_id)[0]
        assert back.error == "ZeroDivisionError: division by zero"

    def test_parent_id_persisted(self, ledger: ExecutionLedger):
        parent = _make_event()
        child = _make_event(
            trace_id=parent.trace_id,
            parent_id=parent.event_id,
        )
        ledger.record_event(parent)
        ledger.record_event(child)

        events = ledger.get_trace(parent.trace_id)
        child_back = next(e for e in events if e.event_id == child.event_id)
        assert child_back.parent_id == parent.event_id

    def test_duplicate_event_id_raises(self, ledger: ExecutionLedger):
        event = _make_event()
        ledger.record_event(event)
        with pytest.raises(Exception):
            ledger.record_event(event)  # PRIMARY KEY violation

    def test_none_output_roundtrip(self, ledger: ExecutionLedger):
        event = _make_event(output=None)
        ledger.record_event(event)
        back = ledger.get_trace(event.trace_id)[0]
        assert back.output is None

    def test_complex_output_roundtrip(self, ledger: ExecutionLedger):
        event = _make_event(output={"items": [1, "two", None, True]})
        ledger.record_event(event)
        back = ledger.get_trace(event.trace_id)[0]
        assert back.output == {"items": [1, "two", None, True]}


# ---------------------------------------------------------------------------
# Retrieval / get_trace tests
# ---------------------------------------------------------------------------

class TestGetTrace:
    def test_returns_empty_list_for_unknown_trace(self, ledger: ExecutionLedger):
        result = ledger.get_trace(str(uuid.uuid4()))
        assert result == []

    def test_returns_only_matching_trace_events(self, ledger: ExecutionLedger):
        tid_a = str(uuid.uuid4())
        tid_b = str(uuid.uuid4())
        for _ in range(3):
            ledger.record_event(_make_event(trace_id=tid_a))
        for _ in range(2):
            ledger.record_event(_make_event(trace_id=tid_b))

        assert len(ledger.get_trace(tid_a)) == 3
        assert len(ledger.get_trace(tid_b)) == 2

    def test_events_ordered_by_started_at_ascending(self, ledger: ExecutionLedger):
        from datetime import timedelta

        trace_id = str(uuid.uuid4())
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Insert out of chronological order
        for offset in [3, 1, 2]:
            ts = base + timedelta(seconds=offset)
            event = ExecutionEvent(
                event_id=str(uuid.uuid4()),
                trace_id=trace_id,
                parent_id=None,
                function_name="f",
                module_path="m",
                inputs={},
                output=None,
                error=None,
                started_at=ts,
                completed_at=ts,
                duration_ms=1.0,
            )
            ledger.record_event(event)

        events = ledger.get_trace(trace_id)
        timestamps = [e.started_at for e in events]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# list_traces tests
# ---------------------------------------------------------------------------

class TestListTraces:
    def test_empty_db_returns_empty_list(self, ledger: ExecutionLedger):
        assert ledger.list_traces() == []

    def test_single_trace_appears(self, ledger: ExecutionLedger):
        event = _make_event()
        ledger.record_event(event)
        summaries = ledger.list_traces()
        assert len(summaries) == 1
        s = summaries[0]
        assert s["trace_id"] == event.trace_id
        assert s["total_events"] == 1
        assert s["has_error"] is False

    def test_multiple_traces_all_appear(self, ledger: ExecutionLedger):
        for _ in range(4):
            ledger.record_event(_make_event())
        summaries = ledger.list_traces()
        assert len(summaries) == 4

    def test_total_events_count_correct(self, ledger: ExecutionLedger):
        tid = str(uuid.uuid4())
        for _ in range(5):
            ledger.record_event(_make_event(trace_id=tid))
        summary = ledger.list_traces()[0]
        assert summary["total_events"] == 5

    def test_has_error_false_when_no_errors(self, ledger: ExecutionLedger):
        event = _make_event(error=None)
        ledger.record_event(event)
        assert ledger.list_traces()[0]["has_error"] is False

    def test_has_error_true_when_any_event_errored(self, ledger: ExecutionLedger):
        tid = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=tid, error=None))
        ledger.record_event(_make_event(trace_id=tid, error="Oops"))
        summary = next(s for s in ledger.list_traces() if s["trace_id"] == tid)
        assert summary["has_error"] is True

    def test_has_error_false_when_all_succeed(self, ledger: ExecutionLedger):
        tid = str(uuid.uuid4())
        for _ in range(3):
            ledger.record_event(_make_event(trace_id=tid, error=None))
        summary = next(s for s in ledger.list_traces() if s["trace_id"] == tid)
        assert summary["has_error"] is False

    def test_started_at_is_earliest_event(self, ledger: ExecutionLedger):
        from datetime import timedelta

        trace_id = str(uuid.uuid4())
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)

        earliest = base
        for offset in [5, 0, 10]:  # earliest is offset=0
            ts = base + timedelta(seconds=offset)
            event = ExecutionEvent(
                event_id=str(uuid.uuid4()),
                trace_id=trace_id,
                parent_id=None,
                function_name="g",
                module_path="n",
                inputs={},
                output=None,
                error=None,
                started_at=ts,
                completed_at=ts,
                duration_ms=1.0,
            )
            ledger.record_event(event)

        summary = next(s for s in ledger.list_traces() if s["trace_id"] == trace_id)
        # Allow a 1-second tolerance for timezone round-trip
        diff = abs((summary["started_at"] - earliest.replace(tzinfo=timezone.utc)).total_seconds())
        assert diff < 1

    def test_list_traces_returns_datetime_objects(self, ledger: ExecutionLedger):
        ledger.record_event(_make_event())
        s = ledger.list_traces()[0]
        assert isinstance(s["started_at"], datetime)

