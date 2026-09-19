"""
examples/order_service.py
=========================
Realistic order-processing pipeline demonstrating RetroTrace Phase 1-2:

  - @record          → pure functions (calculation, orchestrator)
  - @mock_side_effect → external boundaries (payment gateway, inventory DB)
  - ReplayEngine     → deterministic replay without re-firing side effects

Run it directly:

    python examples/order_service.py

The script will:
  1. Process a successful order and persist the trace.
  2. Process a failing order (bad discount code) and persist the crashed trace.
  3. Replay the failed trace deterministically — the payment gateway is NOT
     re-called, but the original exception is reproduced faithfully.
"""

from __future__ import annotations

import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from retrotrace.core.recorder import configure, record
from retrotrace.core.replayer import ReplayEngine, mock_side_effect
from retrotrace.storage.ledger import ExecutionLedger

# Force UTF-8 so Rich box-drawing characters render correctly on Windows.
console = Console(file=open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False))

# ---------------------------------------------------------------------------
# Ledger — stored next to the examples folder so it doesn't pollute root
# ---------------------------------------------------------------------------

DB_PATH = str(Path(__file__).parent / "order_service.db")


def _setup() -> ExecutionLedger:
    """Wipe any previous run's DB and return a fresh ledger."""
    p = Path(DB_PATH)
    if p.exists():
        p.unlink()
    configure(db_path=DB_PATH)          # set the global default
    return ExecutionLedger(db_path=DB_PATH)


# ---------------------------------------------------------------------------
# Domain data
# ---------------------------------------------------------------------------

VALID_DISCOUNTS: Dict[str, float] = {
    "SAVE10": 0.10,
    "HALF":   0.50,
    "VIP20":  0.20,
}

INVENTORY: Dict[str, int] = {
    "SKU-001": 50,
    "SKU-002": 12,
    "SKU-003": 0,   # Out of stock — triggers failure path
}


# ---------------------------------------------------------------------------
# Pure functions  (@record — body always re-executed on replay)
# ---------------------------------------------------------------------------

@record
def calculate_total(
    items: List[Dict[str, Any]],
    discount_code: Optional[str] = None,
) -> Dict[str, float]:
    """
    Sum line-item prices and apply an optional percentage discount.

    Raises ``ValueError`` for unrecognised discount codes.
    """
    subtotal: float = sum(i["price"] * i["qty"] for i in items)

    discount_pct: float = 0.0
    if discount_code:
        if discount_code not in VALID_DISCOUNTS:
            raise ValueError(f"Unknown discount code: {discount_code!r}")
        discount_pct = VALID_DISCOUNTS[discount_code]

    discount_amount = round(subtotal * discount_pct, 2)
    total = round(subtotal - discount_amount, 2)

    return {
        "subtotal":        round(subtotal, 2),
        "discount_code":   discount_code,
        "discount_pct":    discount_pct,
        "discount_amount": discount_amount,
        "total":           total,
    }


# ---------------------------------------------------------------------------
# External side effects  (@mock_side_effect — body SKIPPED on replay)
# ---------------------------------------------------------------------------

@mock_side_effect
def call_payment_gateway(account_id: str, amount: float) -> Dict[str, Any]:
    """
    Simulate a payment-gateway charge (network I/O, idempotency key, etc.).
    Returns a fake transaction receipt.
    """
    console.print(
        f"  [bold yellow]>> PAYMENT GATEWAY CALLED[/bold yellow] "
        f"account={account_id!r}  amount=${amount:.2f}  "
        "[dim](this would hit a real API in production)[/dim]"
    )
    time.sleep(0.05)   # simulate network latency
    return {
        "transaction_id": str(uuid.uuid4()),
        "status":         "charged",
        "amount":         amount,
    }


@mock_side_effect
def reserve_inventory(sku: str, quantity: int) -> Dict[str, Any]:
    """
    Simulate a DB write that decrements stock for *sku*.
    Raises ``RuntimeError`` when stock is insufficient.
    """
    stock = INVENTORY.get(sku, 0)
    if quantity < 0:
        raise ValueError(f"Quantity must be non-negative, got {quantity}")
    if quantity > stock:
        raise RuntimeError(
            f"Insufficient stock for {sku!r}: requested {quantity}, available {stock}"
        )

    console.print(
        f"  [bold cyan]>> INVENTORY RESERVED[/bold cyan] "
        f"sku={sku!r}  qty={quantity}  remaining={stock - quantity}"
        "  [dim](this would write to DB in production)[/dim]"
    )

    INVENTORY[sku] = stock - quantity
    return {"sku": sku, "reserved": quantity, "remaining": stock - quantity}


# ---------------------------------------------------------------------------
# Orchestrator  (@record — pure coordination, drives the two side effects)
# ---------------------------------------------------------------------------

@record
def process_order(order_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Full order pipeline:
      1. Calculate the total (with optional discount).
      2. Charge the payment gateway.
      3. Reserve inventory for each line item.

    Any exception propagates up and is captured by the ledger.
    """
    account_id    = order_payload["account_id"]
    items         = order_payload["items"]
    discount_code = order_payload.get("discount_code")

    # Step 1 — pricing
    pricing = calculate_total(items, discount_code)

    # Step 2 — payment  (side effect: skip on replay)
    receipt = call_payment_gateway(account_id, pricing["total"])

    # Step 3 — inventory (side effect: skip on replay)
    reservations = []
    for item in items:
        res = reserve_inventory(item["sku"], item["qty"])
        reservations.append(res)

    return {
        "order_id":     str(uuid.uuid4()),
        "account_id":   account_id,
        "pricing":      pricing,
        "receipt":      receipt,
        "reservations": reservations,
        "status":       "confirmed",
    }


# ---------------------------------------------------------------------------
# Demo helpers
# ---------------------------------------------------------------------------

def _banner(title: str) -> None:
    console.print()
    console.print(Rule(f"[bold magenta]{title}[/bold magenta]"))
    console.print()


def _show_trace_summary(ledger: ExecutionLedger) -> None:
    traces = ledger.list_traces()
    if not traces:
        return
    last = traces[-1]
    icon = "✗" if last["has_error"] else "✓"
    colour = "red" if last["has_error"] else "green"
    console.print(
        f"  [{colour}]{icon}[/{colour}] "
        f"trace_id=[dim]{last['trace_id']}[/dim]  "
        f"events=[bold]{last['total_events']}[/bold]  "
        f"error=[bold {'red' if last['has_error'] else 'green'}]"
        f"{'yes' if last['has_error'] else 'no'}[/bold {'red' if last['has_error'] else 'green'}]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------

def main() -> None:
    ledger = _setup()

    # ── 1. Successful order ──────────────────────────────────────────────────
    _banner("Demo 1 — Successful Order")

    successful_payload = {
        "account_id":    "acct-42",
        "discount_code": "SAVE10",
        "items": [
            {"sku": "SKU-001", "name": "Wireless Headphones", "price": 79.99, "qty": 2},
            {"sku": "SKU-002", "name": "USB-C Cable",         "price": 12.50, "qty": 3},
        ],
    }

    try:
        result = process_order(successful_payload)
        console.print(
            Panel(
                f"[bold green]Order confirmed![/bold green]\n"
                f"  order_id  : {result['order_id']}\n"
                f"  subtotal  : ${result['pricing']['subtotal']:.2f}\n"
                f"  discount  : -{result['pricing']['discount_pct']*100:.0f}% "
                f"(${result['pricing']['discount_amount']:.2f})\n"
                f"  total     : ${result['pricing']['total']:.2f}\n"
                f"  tx_id     : {result['receipt']['transaction_id']}",
                title="Order Result",
                border_style="green",
            )
        )
    except Exception as exc:
        console.print(f"[red]Unexpected error:[/red] {exc}")

    _show_trace_summary(ledger)
    success_trace_id = ledger.list_traces()[-1]["trace_id"]

    # ── 2. Failing order (bad discount code) ─────────────────────────────────
    _banner("Demo 2 — Failing Order (Invalid Discount Code)")

    failing_payload = {
        "account_id":    "acct-99",
        "discount_code": "BOGUS",        # ← will raise ValueError
        "items": [
            {"sku": "SKU-001", "name": "Wireless Headphones", "price": 79.99, "qty": 1},
        ],
    }

    failing_trace_id: Optional[str] = None
    try:
        process_order(failing_payload)
    except Exception as exc:
        console.print(f"  [bold red]Order failed (expected):[/bold red] {exc}")

    _show_trace_summary(ledger)
    failing_trace_id = ledger.list_traces()[-1]["trace_id"]

    # ── 3. Deterministic replay of the failed trace ───────────────────────────
    _banner("Demo 3 — Deterministic Replay of Failed Trace")

    console.print(
        "  Replaying the failed trace. Note:\n"
        "  • [bold cyan]@mock_side_effect[/bold cyan] functions are "
        "  [bold]skipped[/bold] — no real payment gateway call fires.\n"
        "  • The original [red]ValueError[/red] is reproduced deterministically.\n"
    )

    engine = ReplayEngine(ledger=ledger)
    try:
        engine.replay_trace(failing_trace_id, process_order, failing_payload)
    except RuntimeError as exc:
        console.print(
            Panel(
                f"[bold red]Replay reproduced the original error:[/bold red]\n{exc}",
                border_style="red",
                title="Replay Result",
            )
        )

    # ── Final: CLI hint ───────────────────────────────────────────────────────
    _banner("Explore with the CLI")

    console.print(
        f"  [dim]$ [/dim][bold]retrotrace --db examples/order_service.db list[/bold]\n"
        f"  [dim]$ [/dim][bold]retrotrace --db examples/order_service.db inspect {success_trace_id}[/bold]\n"
        f"  [dim]$ [/dim][bold]retrotrace --db examples/order_service.db diff "
        f"{success_trace_id[:8]}… {failing_trace_id[:8]}…[/bold]\n"
    )

    ledger.close()


if __name__ == "__main__":
    main()
