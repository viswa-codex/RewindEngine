import argparse
import sys
import webbrowser
import uvicorn
from rich.console import Console
from rich.table import Table
from rich.tree import Tree
from rich.panel import Panel

from retrotrace.storage.ledger import ExecutionLedger

console = Console()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retrotrace", description="Deterministic Execution Recorder & Studio")
    parser.add_argument("--db", default=".retrotrace.db", help="Path to SQLite ledger database")

    subparsers = parser.add_subparsers(dest="command")

    # list
    subparsers.add_parser("list", help="List execution traces")

    # inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect a trace execution tree")
    p_inspect.add_argument("trace_id", help="Trace UUID to inspect")

    # diff
    p_diff = subparsers.add_parser("diff", help="Diff two execution traces")
    p_diff.add_argument("t1", help="First trace UUID")
    p_diff.add_argument("t2", help="Second trace UUID")

    # studio
    p_studio = subparsers.add_parser("studio", help="Launch Developer Web Studio")
    p_studio.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    p_studio.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    p_studio.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")

    return parser


def cmd_list(db_path: str):
    ledger = ExecutionLedger(db_path=db_path)
    traces = ledger.list_traces()

    if not traces:
        console.print(f"[yellow]No traces found in database: {db_path}[/yellow]")
        return

    table = Table(title="Execution Traces")
    table.add_column("Status", justify="center")
    table.add_column("Trace ID", style="cyan")
    table.add_column("Root Function", style="bold")
    table.add_column("Started At", style="dim")
    table.add_column("Events", justify="right")
    table.add_column("Error")

    for t in traces:
        events = ledger.get_trace(t["trace_id"])
        root_fn = events[0].fn_name if events else "unknown"
        status = "[red]✗[/red]" if t["has_error"] else "[green]✓[/green]"
        err_text = "[red]Yes[/red]" if t["has_error"] else "[green]No[/green]"

        table.add_row(
            status,
            t["trace_id"],
            root_fn,
            str(t["started_at"]),
            str(t["total_events"]),
            err_text
        )

    console.print(table)


def cmd_inspect(db_path: str, trace_id: str):
    ledger = ExecutionLedger(db_path=db_path)
    events = ledger.get_trace(trace_id)

    if not events:
        console.print(f"[red]Error: Trace {trace_id} not found.[/red]")
        sys.exit(1)

    root_tree = Tree(f"[bold cyan]Trace: {trace_id}[/bold cyan]")
    nodes = {}

    for e in events:
        label = f"[bold]{e.fn_name}[/bold] ({e.duration_ms:.2f}ms)"
        if e.error:
            label += f"\n  [red]Error: {e.error.strip().splitlines()[-1]}[/red]"
        else:
            label += f"\n  [dim]Out: {str(e.output)[:80]}[/dim]"

        if e.parent_id and e.parent_id in nodes:
            node = nodes[e.parent_id].add(label)
        else:
            node = root_tree.add(label)
        nodes[e.event_id] = node

    console.print(Panel(root_tree, title="Execution Tree", expand=False))


def cmd_diff(db_path: str, t1: str, t2: str):
    ledger = ExecutionLedger(db_path=db_path)
    events1 = ledger.get_trace(t1)
    events2 = ledger.get_trace(t2)

    if not events1 or not events2:
        console.print("[red]Error: One or both traces not found.[/red]")
        sys.exit(1)

    table = Table(title=f"Trace Diff: {t1[:8]}... vs {t2[:8]}...")
    table.add_column("Step", justify="center")
    table.add_column("Match", justify="center")
    table.add_column(f"Trace 1 ({t1[:8]})", style="cyan")
    table.add_column(f"Trace 2 ({t2[:8]})", style="magenta")
    table.add_column("Diff Detail", style="yellow")

    max_len = max(len(events1), len(events2))
    for i in range(max_len):
        e1 = events1[i] if i < len(events1) else None
        e2 = events2[i] if i < len(events2) else None

        fn1 = e1.fn_name if e1 else "-"
        fn2 = e2.fn_name if e2 else "-"

        if not e1 or not e2:
            table.add_row(str(i + 1), "[red]±[/red]", fn1, fn2, "Missing step in one run")
        elif e1.fn_name != e2.fn_name:
            table.add_row(str(i + 1), "[red]✗[/red]", fn1, fn2, "Function name mismatch")
        elif e1.inputs != e2.inputs:
            table.add_row(str(i + 1), "[yellow]~[/yellow]", fn1, fn2, "Input arguments drifted")
        elif e1.output != e2.output or e1.error != e2.error:
            table.add_row(str(i + 1), "[yellow]~[/yellow]", fn1, fn2, "Output/Error drifted")
        else:
            table.add_row(str(i + 1), "[green]✓[/green]", fn1, fn2, "Identical")

    console.print(table)


def cmd_studio(db_path: str, host: str, port: int, no_browser: bool):
    from retrotrace.server.app import create_app
    app = create_app(db_path=db_path)
    url = f"http://{host}:{port}"
    console.print(f"[bold green]Starting RetroTrace Studio at {url} (DB: {db_path})[/bold green]")
    if not no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args.db)
    elif args.command == "inspect":
        cmd_inspect(args.db, args.trace_id)
    elif args.command == "diff":
        cmd_diff(args.db, args.t1, args.t2)
    elif args.command == "studio":
        cmd_studio(args.db, args.host, args.port, args.no_browser)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()