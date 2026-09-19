"""
retrotrace/cli/main.py
Rich-powered terminal UI for RetroTrace.

Commands
--------
  retrotrace list [--db PATH]
  retrotrace inspect <trace_id> [--db PATH]
  retrotrace diff <trace_id_1> <trace_id_2> [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from rich import box

from retrotrace.storage.ledger import ExecutionEvent, ExecutionLedger

console = Console()

_DEFAULT_DB = ".retrotrace.db"

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _status_icon(has_error: bool) -> Text:
    if has_error:
        return Text("✗", style="bold red")
    return Text("✓", style="bold green")


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


def _fmt_inputs(inputs: dict) -> str:
    try:
        return json.dumps(inputs, indent=2)
    except Exception:
        return repr(inputs)


def _fmt_output(output) -> str:
    if output is None:
        return "[dim]None[/dim]"
    try:
        return json.dumps(output, indent=2)
    except Exception:
        return repr(output)


def _truncate(s: str, max_len: int = 120) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _error_snippet(error: str, max_lines: int = 4) -> str:
    """Return the last *max_lines* lines of a traceback string."""
    lines = [l for l in error.strip().splitlines() if l.strip()]
    return "\n".join(lines[-max_lines:])


# ---------------------------------------------------------------------------
# Command: list
# ---------------------------------------------------------------------------

def cmd_list(db_path: str) -> None:
    """Display a summary table of all recorded traces."""
    with ExecutionLedger(db_path=db_path) as ledger:
        traces = ledger.list_traces()

    if not traces:
        console.print(
            Panel(
                "[dim]No traces recorded yet.[/dim]\n"
                "Decorate your functions with [bold cyan]@record[/bold cyan] and run them.",
                title="RetroTrace — Traces",
                border_style="dim",
            )
        )
        return

    # Fetch the root function name (first event) for each trace
    root_fn: Dict[str, str] = {}
    with ExecutionLedger(db_path=db_path) as ledger:
        for t in traces:
            events = ledger.get_trace(t["trace_id"])
            root_fn[t["trace_id"]] = events[0].function_name if events else "—"

    table = Table(
        title="RetroTrace — Recorded Traces",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        highlight=True,
    )
    table.add_column("", width=3, justify="center")           # status icon
    table.add_column("Trace ID", style="dim", no_wrap=True)
    table.add_column("Root Function", style="bold")
    table.add_column("Started At", no_wrap=True)
    table.add_column("Events", justify="right")
    table.add_column("Error", style="red")

    for t in traces:
        tid = t["trace_id"]
        has_error = t["has_error"]
        table.add_row(
            _status_icon(has_error),
            tid,
            root_fn.get(tid, "—"),
            _fmt_dt(t["started_at"]),
            str(t["total_events"]),
            "Yes" if has_error else "",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# Command: inspect
# ---------------------------------------------------------------------------

def _build_tree(events: List[ExecutionEvent]) -> Tree:
    """
    Recursively nest events by their parent_id relationships into a Rich Tree.
    Root events (parent_id is None) become top-level branches.
    """
    by_id: Dict[str, ExecutionEvent] = {e.event_id: e for e in events}
    children: Dict[Optional[str], List[ExecutionEvent]] = {}
    for e in events:
        children.setdefault(e.parent_id, []).append(e)

    def _node_label(e: ExecutionEvent) -> Text:
        label = Text()
        label.append(f"{e.function_name}", style="bold yellow")
        label.append(f"  [{e.duration_ms:.2f} ms]", style="dim")
        label.append(f"\n  module: {e.module_path}", style="dim cyan")

        # Inputs
        inputs_str = _truncate(_fmt_inputs(e.inputs), 200)
        label.append(f"\n  inputs: {inputs_str}", style="white")

        if e.error:
            snippet = _error_snippet(e.error)
            label.append(f"\n  [bold red]ERROR:[/bold red] {snippet}", style="red")
        else:
            out = _truncate(_fmt_output(e.output), 120)
            label.append(f"\n  return: {out}", style="green")

        return label

    def _add_children(tree_node, parent_id: Optional[str]) -> None:
        for child in sorted(
            children.get(parent_id, []), key=lambda e: e.started_at
        ):
            branch = tree_node.add(_node_label(child))
            _add_children(branch, child.event_id)

    # Build from roots
    roots = children.get(None, [])
    if not roots:
        # Fallback: treat all as roots in chronological order
        roots = sorted(events, key=lambda e: e.started_at)

    root_roots = sorted(roots, key=lambda e: e.started_at)
    if len(root_roots) == 1:
        tree = Tree(_node_label(root_roots[0]), guide_style="dim")
        _add_children(tree, root_roots[0].event_id)
    else:
        tree = Tree(
            Text("Trace", style="bold magenta"), guide_style="dim"
        )
        for r in root_roots:
            branch = tree.add(_node_label(r))
            _add_children(branch, r.event_id)

    return tree


def cmd_inspect(trace_id: str, db_path: str) -> None:
    """Render a nested tree of all events in a single trace."""
    with ExecutionLedger(db_path=db_path) as ledger:
        events = ledger.get_trace(trace_id)

    if not events:
        console.print(
            f"[bold red]No events found[/bold red] for trace_id [bold]{trace_id}[/bold]"
        )
        sys.exit(1)

    tree = _build_tree(events)
    console.print(
        Panel(
            tree,
            title=f"[bold cyan]Trace:[/bold cyan] {trace_id}",
            border_style="cyan",
            padding=(1, 2),
        )
    )


# ---------------------------------------------------------------------------
# Command: diff
# ---------------------------------------------------------------------------

def cmd_diff(trace_id_1: str, trace_id_2: str, db_path: str) -> None:
    """Side-by-side diff of two execution traces."""
    with ExecutionLedger(db_path=db_path) as ledger:
        events_a = ledger.get_trace(trace_id_1)
        events_b = ledger.get_trace(trace_id_2)

    if not events_a:
        console.print(f"[red]Trace not found:[/red] {trace_id_1}")
        sys.exit(1)
    if not events_b:
        console.print(f"[red]Trace not found:[/red] {trace_id_2}")
        sys.exit(1)

    max_len = max(len(events_a), len(events_b))

    table = Table(
        title="RetroTrace — Trace Diff",
        box=box.ROUNDED,
        header_style="bold cyan",
        show_header=True,
        show_lines=True,
    )
    table.add_column("#", width=4, justify="right", style="dim")
    table.add_column(f"Trace A  [{trace_id_1[:8]}…]", ratio=1)
    table.add_column("Match", width=5, justify="center")
    table.add_column(f"Trace B  [{trace_id_2[:8]}…]", ratio=1)

    any_diff = False

    for i in range(max_len):
        ea = events_a[i] if i < len(events_a) else None
        eb = events_b[i] if i < len(events_b) else None

        # Determine match state
        if ea is None or eb is None:
            match_icon = Text("±", style="bold yellow")
            row_style = "on dark_orange3"
            any_diff = True
        elif ea.function_name != eb.function_name:
            match_icon = Text("✗", style="bold red")
            row_style = "on dark_red"
            any_diff = True
        elif ea.inputs != eb.inputs:
            match_icon = Text("~", style="bold yellow")
            row_style = "on dark_orange3"
            any_diff = True
        elif ea.output != eb.output:
            match_icon = Text("~", style="bold yellow")
            row_style = "on dark_orange3"
            any_diff = True
        else:
            match_icon = Text("✓", style="bold green")
            row_style = ""

        def _cell(e: Optional[ExecutionEvent]) -> Text:
            if e is None:
                return Text("— (missing)", style="dim red")
            t = Text()
            t.append(e.function_name, style="bold yellow")
            t.append(f"  [{e.duration_ms:.2f} ms]\n", style="dim")
            t.append("in:  ", style="dim")
            t.append(_truncate(json.dumps(e.inputs), 80), style="white")
            t.append("\nout: ", style="dim")
            if e.error:
                t.append(_truncate(_error_snippet(e.error, 2), 80), style="red")
            else:
                t.append(_truncate(_fmt_output(e.output), 80), style="green")
            return t

        table.add_row(
            str(i + 1),
            _cell(ea),
            match_icon,
            _cell(eb),
            style=row_style,
        )

    console.print(table)

    if any_diff:
        console.print("\n[bold yellow]⚠  Differences detected between the two traces.[/bold yellow]")
    else:
        console.print("\n[bold green]✓  Traces are identical.[/bold green]")


# ---------------------------------------------------------------------------
# Argument parser & entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retrotrace",
        description="RetroTrace — execution tracing & deterministic replay",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=_DEFAULT_DB,
        help=f"Path to the SQLite ledger (default: {_DEFAULT_DB})",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # ── list ──────────────────────────────────────────────────────────────
    sub.add_parser("list", help="List all recorded traces")

    # ── inspect ───────────────────────────────────────────────────────────
    p_inspect = sub.add_parser("inspect", help="Inspect a single trace as a tree")
    p_inspect.add_argument("trace_id", help="UUID of the trace to inspect")

    # ── diff ──────────────────────────────────────────────────────────────
    p_diff = sub.add_parser("diff", help="Side-by-side diff of two traces")
    p_diff.add_argument("trace_id_1", help="First trace UUID")
    p_diff.add_argument("trace_id_2", help="Second trace UUID")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Sub-command --db overrides global --db
    db = getattr(args, "db", None) or _DEFAULT_DB

    try:
        if args.command == "list":
            cmd_list(db)
        elif args.command == "inspect":
            cmd_inspect(args.trace_id, db)
        elif args.command == "diff":
            cmd_diff(args.trace_id_1, args.trace_id_2, db)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(130)


if __name__ == "__main__":
    main()
