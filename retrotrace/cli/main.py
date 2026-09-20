"""
retrotrace/cli/main.py
Unified CLI entry point for RetroTrace.

Commands: list, inspect, diff, studio
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from retrotrace.storage.ledger import ExecutionLedger

console = Console()

_DEFAULT_DB = ".retrotrace.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status_icon(has_error: bool):
    from rich.text import Text
    return Text("✗", style="bold red") if has_error else Text("✓", style="bold green")


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


def _truncate(s: str, n: int = 120) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_list(db_path: str) -> None:
    with ExecutionLedger(db_path=db_path) as ledger:
        traces = ledger.list_traces()

    if not traces:
        console.print(Panel(
            "[dim]No traces recorded yet.[/dim]\n"
            "Decorate your functions with [bold cyan]@record[/bold cyan] and run them.",
            title="RetroTrace — Traces", border_style="dim",
        ))
        return

    with ExecutionLedger(db_path=db_path) as ledger:
        root_fn = {}
        for t in traces:
            evs = ledger.get_trace(t["trace_id"])
            root_fn[t["trace_id"]] = evs[0].function_name if evs else "—"

    table = Table(title="RetroTrace — Recorded Traces", box=box.ROUNDED,
                  header_style="bold cyan", highlight=True)
    table.add_column("", width=3, justify="center")
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
# inspect
# ---------------------------------------------------------------------------

def cmd_inspect(db_path: str, trace_id: str) -> None:
    with ExecutionLedger(db_path=db_path) as ledger:
        events = ledger.get_trace(trace_id)

    if not events:
        console.print(f"[bold red]No events found[/bold red] for trace [bold]{trace_id}[/bold]")
        sys.exit(1)

    root_tree = Tree(f"[bold cyan]Trace: {trace_id}[/bold cyan]")
    nodes = {}
    for e in events:
        label = f"[bold]{e.function_name}[/bold] ({e.duration_ms:.2f}ms)"
        if e.error:
            last_line = e.error.strip().splitlines()[-1]
            label += f"\n  [red]Error: {last_line}[/red]"
        else:
            label += f"\n  [dim]Out: {_truncate(str(e.output), 80)}[/dim]"

        parent = nodes.get(e.parent_id) if e.parent_id else None
        node = (parent or root_tree).add(label)
        nodes[e.event_id] = node

    console.print(Panel(root_tree, title="Execution Tree", expand=False))


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def cmd_diff(db_path: str, t1: str, t2: str) -> None:
    with ExecutionLedger(db_path=db_path) as ledger:
        events1 = ledger.get_trace(t1)
        events2 = ledger.get_trace(t2)

    if not events1 or not events2:
        console.print("[red]Error: One or both traces not found.[/red]")
        sys.exit(1)

    table = Table(title=f"Trace Diff: {t1[:8]}… vs {t2[:8]}…",
                  box=box.ROUNDED, header_style="bold cyan", show_lines=True)
    table.add_column("Step", justify="center", width=5)
    table.add_column("Match", justify="center", width=5)
    table.add_column(f"Trace A ({t1[:8]})", ratio=1)
    table.add_column(f"Trace B ({t2[:8]})", ratio=1)
    table.add_column("Detail", style="yellow", ratio=1)

    max_len = max(len(events1), len(events2))
    for i in range(max_len):
        e1 = events1[i] if i < len(events1) else None
        e2 = events2[i] if i < len(events2) else None
        fn1 = e1.function_name if e1 else "—"
        fn2 = e2.function_name if e2 else "—"

        if not e1 or not e2:
            icon, detail = "[red]±[/red]", "Missing step"
        elif e1.function_name != e2.function_name:
            icon, detail = "[red]✗[/red]", "Function mismatch"
        elif e1.inputs != e2.inputs:
            icon, detail = "[yellow]~[/yellow]", "Input drift"
        elif e1.output != e2.output or e1.error != e2.error:
            icon, detail = "[yellow]~[/yellow]", "Output/error drift"
        else:
            icon, detail = "[green]✓[/green]", "Identical"

        table.add_row(str(i + 1), icon, fn1, fn2, detail)

    console.print(table)


# ---------------------------------------------------------------------------
# studio
# ---------------------------------------------------------------------------

def cmd_studio(db_path: str, host: str, port: int, no_browser: bool) -> None:
    import threading

    try:
        import uvicorn
        from retrotrace.server.app import create_app
    except ImportError as exc:
        console.print(f"[red]Missing dependency:[/red] {exc}\n"
                      "Run [bold]pip install -e .[/bold] to add FastAPI/uvicorn.")
        sys.exit(1)

    url = f"http://{host}:{port}"
    console.print(
        f"\n[bold cyan]RetroTrace Developer Studio[/bold cyan]\n"
        f"  Studio   : [link={url}]{url}[/link]\n"
        f"  API docs : [link={url}/api/docs]{url}/api/docs[/link]\n"
        f"  DB       : [dim]{db_path}[/dim]\n\n"
        f"Press [bold]Ctrl-C[/bold] to stop.\n"
    )

    app = create_app(db_path=db_path)

    if not no_browser:
        def _open():
            import time
            time.sleep(1.0)
            import webbrowser
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host=host, port=port, log_level="info")


# ---------------------------------------------------------------------------
# Argument parser (exported for tests)
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retrotrace",
        description="RetroTrace — execution tracing & deterministic replay",
    )
    parser.add_argument(
        "--db", metavar="PATH", default=_DEFAULT_DB,
        help=f"Path to the SQLite ledger (default: {_DEFAULT_DB})",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # list
    sub.add_parser("list", help="List all recorded traces")

    # inspect
    p_inspect = sub.add_parser("inspect", help="Inspect a single trace as a tree")
    p_inspect.add_argument("trace_id", help="UUID of the trace to inspect")

    # diff
    p_diff = sub.add_parser("diff", help="Side-by-side diff of two traces")
    p_diff.add_argument("trace_id_1", help="First trace UUID")
    p_diff.add_argument("trace_id_2", help="Second trace UUID")

    # studio
    p_studio = sub.add_parser(
        "studio",
        help="Launch the RetroTrace Developer Studio web UI",
    )
    p_studio.add_argument("--host", default="127.0.0.1", metavar="HOST",
                          help="Host interface (default: 127.0.0.1)")
    p_studio.add_argument("--port", type=int, default=8000, metavar="PORT",
                          help="Port (default: 8000)")
    p_studio.add_argument("--no-browser", action="store_true",
                          help="Do not open browser automatically")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    db = args.db or _DEFAULT_DB

    try:
        if args.command == "list":
            cmd_list(db)
        elif args.command == "inspect":
            cmd_inspect(db, args.trace_id)
        elif args.command == "diff":
            cmd_diff(db, args.trace_id_1, args.trace_id_2)
        elif args.command == "studio":
            cmd_studio(db, args.host, args.port, args.no_browser)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(130)


if __name__ == "__main__":
    main()