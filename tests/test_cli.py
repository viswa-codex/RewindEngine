"""
tests/test_cli.py
Unit tests for retrotrace.cli.main — list, inspect, and diff commands.

Strategy
--------
We exercise each command by calling its internal function directly
(cmd_list / cmd_inspect / cmd_diff), redirecting Rich output to a
StringIO buffer so we can make structural assertions on the rendered text
without relying on ANSI codes or terminal width.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest
from rich.console import Console

from retrotrace.cli.main import (
    _build_parser,
    _build_tree,
    cmd_diff,
    cmd_inspect,
    cmd_list,
)
from retrotrace.storage.ledger import ExecutionEvent, ExecutionLedger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture(fn, *args, **kwargs) -> str:
    """Run a CLI command function with Rich output redirected to a string."""
    buf = io.StringIO()
    test_console = Console(file=buf, width=200, highlight=False, markup=True)

    import retrotrace.cli.main as cli_mod
    original = cli_mod.console
    cli_mod.console = test_console
    try:
        fn(*args, **kwargs)
    except SystemExit:
        pass
    finally:
        cli_mod.console = original

    return buf.getvalue()


def _now(offset_secs: float = 0.0) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=offset_secs)


def _make_event(
    *,
    trace_id: str,
    function_name: str = "fn",
    module_path: str = "mod",
    parent_id: str | None = None,
    inputs: dict | None = None,
    output=42,
    error: str | None = None,
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


@pytest.fixture()
def ledger(tmp_path: Path) -> ExecutionLedger:
    db_file = tmp_path / "cli_test.db"
    with ExecutionLedger(db_path=str(db_file)) as lg:
        yield lg


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

class TestParser:
    def test_list_command_parsed(self):
        parser = _build_parser()
        args = parser.parse_args(["list"])
        assert args.command == "list"

    def test_inspect_command_parsed(self):
        parser = _build_parser()
        args = parser.parse_args(["inspect", "abc-123"])
        assert args.command == "inspect"
        assert args.trace_id == "abc-123"

    def test_diff_command_parsed(self):
        parser = _build_parser()
        args = parser.parse_args(["diff", "t1", "t2"])
        assert args.command == "diff"
        assert args.trace_id_1 == "t1"
        assert args.trace_id_2 == "t2"

    def test_db_override(self):
        parser = _build_parser()
        args = parser.parse_args(["--db", "mydb.db", "list"])
        assert args.db == "mydb.db"

    def test_missing_command_exits(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------

class TestCmdList:
    def test_empty_ledger_shows_no_traces_message(self, ledger, tmp_path):
        output = _capture(cmd_list, str(tmp_path / "cli_test.db"))
        assert "No traces recorded yet" in output

    def test_populated_ledger_shows_trace_rows(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=tid, function_name="pipeline"))
        output = _capture(cmd_list, str(tmp_path / "cli_test.db"))
        assert tid in output
        assert "pipeline" in output

    def test_success_trace_shows_checkmark(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=tid, error=None))
        output = _capture(cmd_list, str(tmp_path / "cli_test.db"))
        assert "✓" in output

    def test_error_trace_shows_cross(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(
            _make_event(trace_id=tid, error="ValueError: bad\n  line2")
        )
        output = _capture(cmd_list, str(tmp_path / "cli_test.db"))
        assert "✗" in output
        assert "Yes" in output

    def test_event_count_shown(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        for _ in range(3):
            ledger.record_event(_make_event(trace_id=tid))
        output = _capture(cmd_list, str(tmp_path / "cli_test.db"))
        assert "3" in output

    def test_multiple_traces_all_listed(self, ledger, tmp_path):
        for fn_name in ["alpha", "beta", "gamma"]:
            ledger.record_event(
                _make_event(trace_id=str(uuid.uuid4()), function_name=fn_name)
            )
        output = _capture(cmd_list, str(tmp_path / "cli_test.db"))
        for fn_name in ["alpha", "beta", "gamma"]:
            assert fn_name in output

    def test_table_header_columns_present(self, ledger, tmp_path):
        ledger.record_event(_make_event(trace_id=str(uuid.uuid4())))
        output = _capture(cmd_list, str(tmp_path / "cli_test.db"))
        assert "Trace ID" in output
        assert "Root Function" in output
        assert "Events" in output


# ---------------------------------------------------------------------------
# cmd_inspect
# ---------------------------------------------------------------------------

class TestCmdInspect:
    def test_single_event_shows_function_name(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=tid, function_name="my_fn"))
        output = _capture(cmd_inspect, tid, str(tmp_path / "cli_test.db"))
        assert "my_fn" in output

    def test_duration_shown(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=tid, duration_ms=37.5))
        output = _capture(cmd_inspect, tid, str(tmp_path / "cli_test.db"))
        assert "37.50 ms" in output

    def test_inputs_rendered(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(
            _make_event(trace_id=tid, inputs={"amount": 100, "currency": "USD"})
        )
        output = _capture(cmd_inspect, tid, str(tmp_path / "cli_test.db"))
        assert "amount" in output
        assert "100" in output

    def test_return_value_shown(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=tid, output={"result": 42}))
        output = _capture(cmd_inspect, tid, str(tmp_path / "cli_test.db"))
        assert "42" in output

    def test_error_shown_in_red_label(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(
            _make_event(
                trace_id=tid,
                error="Traceback (most recent call last):\n  ...\nValueError: oops",
            )
        )
        output = _capture(cmd_inspect, tid, str(tmp_path / "cli_test.db"))
        assert "ERROR" in output or "ValueError" in output

    def test_unknown_trace_id_exits(self, ledger, tmp_path):
        """cmd_inspect with a missing trace_id calls sys.exit(1)."""
        fake_id = str(uuid.uuid4())
        # Should not raise anything to the test — SystemExit is caught by _capture
        output = _capture(cmd_inspect, fake_id, str(tmp_path / "cli_test.db"))
        assert "No events found" in output

    def test_nested_events_both_shown(self, ledger, tmp_path):
        """A parent + child event should both appear in the tree output."""
        tid = str(uuid.uuid4())
        parent = _make_event(
            trace_id=tid, function_name="outer", started_offset=0
        )
        child = _make_event(
            trace_id=tid,
            function_name="inner",
            parent_id=parent.event_id,
            started_offset=0.001,
        )
        ledger.record_event(parent)
        ledger.record_event(child)
        output = _capture(cmd_inspect, tid, str(tmp_path / "cli_test.db"))
        assert "outer" in output
        assert "inner" in output

    def test_trace_id_shown_in_panel_title(self, ledger, tmp_path):
        tid = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=tid))
        output = _capture(cmd_inspect, tid, str(tmp_path / "cli_test.db"))
        # At least the first 8 chars of the UUID should appear
        assert tid[:8] in output


# ---------------------------------------------------------------------------
# _build_tree (unit-level)
# ---------------------------------------------------------------------------

class TestBuildTree:
    def test_single_event_tree_not_none(self):
        tid = str(uuid.uuid4())
        events = [_make_event(trace_id=tid, function_name="root")]
        tree = _build_tree(events)
        assert tree is not None

    def test_nested_events_produce_children(self):
        """The tree for parent→child must include both function names in label."""
        tid = str(uuid.uuid4())
        parent = _make_event(trace_id=tid, function_name="parent_fn")
        child = _make_event(
            trace_id=tid, function_name="child_fn", parent_id=parent.event_id
        )
        tree = _build_tree([parent, child])
        # Rich Tree's label is a Text object; convert to plain str
        root_label = tree.label.plain if hasattr(tree.label, "plain") else str(tree.label)
        assert "parent_fn" in root_label
        # Children present
        assert len(tree.children) == 1
        child_label = tree.children[0].label.plain
        assert "child_fn" in child_label

    def test_multiple_roots(self):
        """Two events with no parent_id become two top-level branches."""
        tid = str(uuid.uuid4())
        e1 = _make_event(trace_id=tid, function_name="a", started_offset=0)
        e2 = _make_event(trace_id=tid, function_name="b", started_offset=1)
        tree = _build_tree([e1, e2])
        # The umbrella "Trace" root has 2 children
        assert len(tree.children) == 2


# ---------------------------------------------------------------------------
# cmd_diff
# ---------------------------------------------------------------------------

class TestCmdDiff:
    def _two_traces(self, ledger) -> tuple[str, str]:
        """Record two identical traces; return (trace_id_1, trace_id_2)."""
        tids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for tid in tids:
            ledger.record_event(
                _make_event(trace_id=tid, function_name="compute", inputs={"n": 5}, output=10)
            )
        return tids[0], tids[1]

    def test_identical_traces_show_checkmark(self, ledger, tmp_path):
        t1, t2 = self._two_traces(ledger)
        output = _capture(cmd_diff, t1, t2, str(tmp_path / "cli_test.db"))
        assert "✓" in output

    def test_identical_traces_no_diff_message(self, ledger, tmp_path):
        t1, t2 = self._two_traces(ledger)
        output = _capture(cmd_diff, t1, t2, str(tmp_path / "cli_test.db"))
        assert "identical" in output.lower() or "Traces are identical" in output

    def test_different_function_shows_cross(self, ledger, tmp_path):
        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=t1, function_name="alpha"))
        ledger.record_event(_make_event(trace_id=t2, function_name="beta"))
        output = _capture(cmd_diff, t1, t2, str(tmp_path / "cli_test.db"))
        assert "✗" in output

    def test_different_inputs_shows_tilde(self, ledger, tmp_path):
        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        ledger.record_event(
            _make_event(trace_id=t1, function_name="fn", inputs={"x": 1})
        )
        ledger.record_event(
            _make_event(trace_id=t2, function_name="fn", inputs={"x": 99})
        )
        output = _capture(cmd_diff, t1, t2, str(tmp_path / "cli_test.db"))
        assert "~" in output

    def test_different_outputs_shows_tilde(self, ledger, tmp_path):
        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        ledger.record_event(
            _make_event(trace_id=t1, function_name="fn", inputs={"x": 1}, output=10)
        )
        ledger.record_event(
            _make_event(trace_id=t2, function_name="fn", inputs={"x": 1}, output=99)
        )
        output = _capture(cmd_diff, t1, t2, str(tmp_path / "cli_test.db"))
        assert "~" in output

    def test_unequal_length_traces_show_missing(self, ledger, tmp_path):
        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        for i in range(3):
            ledger.record_event(
                _make_event(trace_id=t1, function_name="fn", started_offset=i * 0.001)
            )
        ledger.record_event(_make_event(trace_id=t2, function_name="fn"))
        output = _capture(cmd_diff, t1, t2, str(tmp_path / "cli_test.db"))
        assert "missing" in output.lower() or "±" in output

    def test_diff_shows_differences_warning(self, ledger, tmp_path):
        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=t1, function_name="x"))
        ledger.record_event(_make_event(trace_id=t2, function_name="y"))
        output = _capture(cmd_diff, t1, t2, str(tmp_path / "cli_test.db"))
        assert "Differences detected" in output or "difference" in output.lower()

    def test_missing_trace_exits(self, ledger, tmp_path):
        t1 = str(uuid.uuid4())
        fake = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=t1))
        output = _capture(cmd_diff, t1, fake, str(tmp_path / "cli_test.db"))
        assert "not found" in output.lower() or "Trace not found" in output

    def test_both_function_names_in_output(self, ledger, tmp_path):
        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        ledger.record_event(_make_event(trace_id=t1, function_name="func_a"))
        ledger.record_event(_make_event(trace_id=t2, function_name="func_b"))
        output = _capture(cmd_diff, t1, t2, str(tmp_path / "cli_test.db"))
        assert "func_a" in output
        assert "func_b" in output

