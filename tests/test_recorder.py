"""
tests/test_recorder.py
Unit tests for retrotrace.core.recorder — the @record decorator.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from retrotrace.core.recorder import record, configure, current_trace_id, current_parent_id
from retrotrace.storage.ledger import ExecutionLedger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    """Fresh ledger backed by a temp file, passed explicitly to each @record."""
    db_file = tmp_path / "recorder_test.db"
    with ExecutionLedger(db_path=db_file) as lg:
        yield lg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_event(ledger: ExecutionLedger, trace_id: str):
    events = ledger.get_trace(trace_id)
    assert events, "No events were recorded"
    return events[0]


# ---------------------------------------------------------------------------
# 1. Basic synchronous function recording
# ---------------------------------------------------------------------------

class TestSyncRecording:
    def test_basic_inputs_and_output(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def add(x: int, y: int) -> int:
            return x + y

        result = add(3, 4)
        assert result == 7

        traces = ledger.list_traces()
        assert len(traces) == 1
        event = _first_event(ledger, traces[0]["trace_id"])

        assert event.function_name == "add"
        assert event.inputs == {"x": 3, "y": 4}
        assert event.output == 7
        assert event.error is None

    def test_default_argument_captured(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def greet(name: str, greeting: str = "Hello") -> str:
            return f"{greeting}, {name}!"

        greet("World")
        traces = ledger.list_traces()
        event = _first_event(ledger, traces[0]["trace_id"])
        # Default value must be bound and captured
        assert event.inputs["greeting"] == "Hello"
        assert event.inputs["name"] == "World"

    def test_timing_fields_populated(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def noop():
            pass

        noop()
        traces = ledger.list_traces()
        event = _first_event(ledger, traces[0]["trace_id"])

        assert event.started_at <= event.completed_at
        assert event.duration_ms >= 0

    def test_return_value_none_recorded(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def nothing():
            return None

        nothing()
        traces = ledger.list_traces()
        event = _first_event(ledger, traces[0]["trace_id"])
        assert event.output is None

    def test_decorator_preserves_function_name(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def my_function():
            pass

        assert my_function.__name__ == "my_function"

    def test_bare_decorator_without_parens(self, ledger: ExecutionLedger):
        """@record with no parentheses must still work when given a ledger via configure."""
        configure(db_path=str(tmp := Path(ledger._db_path)))

        @record
        def double(n: int) -> int:
            return n * 2

        result = double(5)
        assert result == 10


# ---------------------------------------------------------------------------
# 2. Async function recording
# ---------------------------------------------------------------------------

class TestAsyncRecording:
    @pytest.mark.asyncio
    async def test_async_inputs_and_output(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        async def async_add(x: int, y: int) -> int:
            return x + y

        result = await async_add(10, 20)
        assert result == 30

        traces = ledger.list_traces()
        event = _first_event(ledger, traces[0]["trace_id"])
        assert event.function_name == "async_add"
        assert event.inputs == {"x": 10, "y": 20}
        assert event.output == 30
        assert event.error is None

    @pytest.mark.asyncio
    async def test_async_timing(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        async def slow():
            await asyncio.sleep(0.05)

        await slow()
        traces = ledger.list_traces()
        event = _first_event(ledger, traces[0]["trace_id"])
        assert event.duration_ms >= 40  # allow generous tolerance

    @pytest.mark.asyncio
    async def test_async_preserves_return_value(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        async def echo(msg: str) -> str:
            return msg

        result = await echo("hello")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_async_decorator_preserves_function_name(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        async def my_async():
            pass

        assert my_async.__name__ == "my_async"


# ---------------------------------------------------------------------------
# 3. Nested calls — parent_id propagation and shared trace_id
# ---------------------------------------------------------------------------

class TestNestedCalls:
    def test_shared_trace_id(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def inner(x):
            return x * 2

        @record(ledger=ledger)
        def outer(x):
            return inner(x)

        outer(5)
        traces = ledger.list_traces()
        assert len(traces) == 1, "Both calls must share one trace"

        events = ledger.get_trace(traces[0]["trace_id"])
        assert len(events) == 2
        trace_ids = {e.trace_id for e in events}
        assert len(trace_ids) == 1, "trace_id must be identical for all events"

    def test_parent_id_propagation(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def inner(x):
            return x + 1

        @record(ledger=ledger)
        def outer(x):
            return inner(x)

        outer(3)
        traces = ledger.list_traces()
        events = {e.function_name: e for e in ledger.get_trace(traces[0]["trace_id"])}

        outer_event = events["outer"]
        inner_event = events["inner"]

        # outer has no parent (it is the root)
        assert outer_event.parent_id is None
        # inner's parent_id must equal outer's event_id
        assert inner_event.parent_id == outer_event.event_id

    def test_three_level_nesting(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def level_c():
            return "c"

        @record(ledger=ledger)
        def level_b():
            return level_c()

        @record(ledger=ledger)
        def level_a():
            return level_b()

        level_a()
        traces = ledger.list_traces()
        events = {e.function_name: e for e in ledger.get_trace(traces[0]["trace_id"])}

        assert events["level_b"].parent_id == events["level_a"].event_id
        assert events["level_c"].parent_id == events["level_b"].event_id

    def test_context_reset_after_call(self, ledger: ExecutionLedger):
        """After a recorded call completes, context vars must return to their
        pre-call state so that sibling calls are independent."""
        @record(ledger=ledger)
        def sibling_a():
            pass

        @record(ledger=ledger)
        def sibling_b():
            pass

        # Call sibling_a first — it generates trace_id A
        sibling_a()
        trace_a = ledger.list_traces()[0]["trace_id"]

        # Call sibling_b outside any active trace — new root, new trace_id
        sibling_b()
        all_traces = {t["trace_id"] for t in ledger.list_traces()}
        assert len(all_traces) == 2, (
            "sibling_b must produce its own trace, not inherit sibling_a's"
        )

    @pytest.mark.asyncio
    async def test_async_nested_parent_id(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        async def a_inner(v):
            return v

        @record(ledger=ledger)
        async def a_outer(v):
            return await a_inner(v)

        await a_outer(99)
        traces = ledger.list_traces()
        events = {e.function_name: e for e in ledger.get_trace(traces[0]["trace_id"])}

        assert events["a_inner"].parent_id == events["a_outer"].event_id


# ---------------------------------------------------------------------------
# 4. Exception handling
# ---------------------------------------------------------------------------

class TestExceptionHandling:
    def test_exception_captured_in_error_field(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def boom():
            raise ValueError("something went wrong")

        with pytest.raises(ValueError, match="something went wrong"):
            boom()

        traces = ledger.list_traces()
        assert traces[0]["has_error"] is True

        event = _first_event(ledger, traces[0]["trace_id"])
        assert event.error is not None
        assert "ValueError" in event.error
        assert "something went wrong" in event.error

    def test_exception_is_reraised(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def explode():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            explode()

    def test_event_stored_even_on_exception(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def faulty(x):
            raise TypeError("bad type")

        with pytest.raises(TypeError):
            faulty(42)

        # Event must still be persisted
        traces = ledger.list_traces()
        assert len(traces) == 1
        event = _first_event(ledger, traces[0]["trace_id"])
        assert event.inputs == {"x": 42}

    def test_output_is_none_on_exception(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def crash():
            raise Exception("crash")

        with pytest.raises(Exception):
            crash()

        event = _first_event(ledger, ledger.list_traces()[0]["trace_id"])
        assert event.output is None

    @pytest.mark.asyncio
    async def test_async_exception_captured(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        async def async_boom():
            raise ZeroDivisionError("async divide by zero")

        with pytest.raises(ZeroDivisionError):
            await async_boom()

        traces = ledger.list_traces()
        event = _first_event(ledger, traces[0]["trace_id"])
        assert event.error is not None
        assert "ZeroDivisionError" in event.error


# ---------------------------------------------------------------------------
# 5. Non-serialisable arguments
# ---------------------------------------------------------------------------

class TestNonSerializableArgs:
    def test_custom_object_does_not_crash(self, ledger: ExecutionLedger):
        class MyObject:
            def __repr__(self):
                return "<MyObject instance>"

        @record(ledger=ledger)
        def process(obj):
            return "ok"

        obj = MyObject()
        result = process(obj)
        assert result == "ok"

        event = _first_event(ledger, ledger.list_traces()[0]["trace_id"])
        # inputs must contain the repr string, not crash
        assert event.inputs["obj"] == repr(obj)

    def test_lambda_as_argument_serialised(self, ledger: ExecutionLedger):
        @record(ledger=ledger)
        def apply(fn, x):
            return fn(x)

        apply(lambda v: v * 2, 3)
        # Should not raise; inputs["fn"] is a repr string
        event = _first_event(ledger, ledger.list_traces()[0]["trace_id"])
        assert isinstance(event.inputs["fn"], str)

    def test_nested_non_serialisable_dict(self, ledger: ExecutionLedger):
        class Blob:
            pass

        @record(ledger=ledger)
        def take_dict(d: dict):
            return "done"

        take_dict({"key": Blob()})
        event = _first_event(ledger, ledger.list_traces()[0]["trace_id"])
        assert "key" in event.inputs["d"]

    def test_non_serialisable_return_value(self, ledger: ExecutionLedger):
        class BigObj:
            def __repr__(self):
                return "<BigObj>"

        @record(ledger=ledger)
        def make_obj():
            return BigObj()

        result = make_obj()
        assert isinstance(result, BigObj)  # original value returned unchanged

        event = _first_event(ledger, ledger.list_traces()[0]["trace_id"])
        assert event.output == "<BigObj>"

