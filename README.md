# RetroTrace

![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-98%20passed-brightgreen?logo=pytest)
![License](https://img.shields.io/badge/license-MIT-blue)
![SQLite](https://img.shields.io/badge/storage-SQLite-lightgrey?logo=sqlite)
![Rich](https://img.shields.io/badge/TUI-Rich-purple)

> **Deterministic execution recorder and time-travel replay engine for Python.**  
> Record any function call tree once. Replay it forever — without touching your database, payment gateway, or any other side effect.

---

## Why RetroTrace?

Production bugs are almost always unreproducible. By the time you attach a debugger, the state is gone, the Stripe charge has fired, and your logs have 40 irrelevant lines between the two that matter.

RetroTrace solves this by **capturing the complete execution graph** — every function call, every argument, every return value, every exception — into an append-only SQLite ledger. From that snapshot you can:

- **Replay** the exact crash, down to the millisecond, without re-charging the card.
- **Diff** two production runs side-by-side to spot when a regression crept in.
- **Inspect** the full nested call tree visually in your terminal.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Your Application                             │
│                                                                     │
│   @record            @record           @mock_side_effect            │
│  ┌─────────┐        ┌──────────┐      ┌──────────────────┐         │
│  │process_ │──────► │calculate │      │call_payment_     │         │
│  │order()  │        │_total()  │      │gateway()         │         │
│  └────┬────┘        └──────────┘      └────────┬─────────┘         │
│       │                                         │                   │
└───────┼─────────────────────────────────────────┼───────────────────┘
        │                                         │
        ▼                                         ▼
┌───────────────────────────────────────────────────────┐
│                   ExecutionLedger                      │
│             (append-only SQLite, Pydantic v2)          │
│                                                        │
│  event_id  trace_id  parent_id  function_name  inputs │
│  output    error     started_at  duration_ms   ...    │
└───────────────┬───────────────────────────────────────┘
                │
        ┌───────┴────────┐
        ▼                ▼
  ReplayEngine      Rich TUI CLI
  (deterministic    retrotrace list
   replay without   retrotrace inspect
   side effects)    retrotrace diff
```

### Context propagation

```
process_order()          ← trace_id = "abc"  parent_id = None
  │
  ├─ calculate_total()   ← trace_id = "abc"  parent_id = process_order.event_id
  │
  ├─ call_payment_gateway()   ← trace_id = "abc"  parent_id = process_order.event_id
  │    └── @mock_side_effect: SKIPPED during replay
  │
  └─ reserve_inventory()      ← trace_id = "abc"  parent_id = process_order.event_id
       └── @mock_side_effect: SKIPPED during replay
```

Two [`contextvars.ContextVar`](https://docs.python.org/3/library/contextvars.html) objects — `current_trace_id` and `current_parent_id` — propagate transparently across sync and async boundaries with zero user configuration.

---

## Core Features

| Feature | Details |
|---|---|
| **`@record`** | Intercepts sync & async functions; captures inputs (with default binding), output, timing, and full exception traceback |
| **`@mock_side_effect`** | Marks external boundaries (APIs, DBs); live runs record normally, replays skip the body entirely |
| **Append-only ledger** | SQLite-backed via Pydantic v2 models; indexed on `trace_id` + `event_id`; thread-safe |
| **Deterministic replay** | `ReplayEngine` matches call sequence, validates argument identity, reproduces exceptions |
| **Drift detection** | `ExecutionDriftError` raised instantly when arguments diverge during replay |
| **Rich TUI** | `retrotrace list / inspect / diff` — coloured tables, nested call trees, side-by-side diffs |
| **Safe serialisation** | Non-serialisable objects fall back to `repr()`; recording never crashes the host app |
| **Async-native** | Full coroutine support: `async def` functions, async replay, `replay_trace_async()` |

---

## Quickstart

### Installation

```bash
git clone https://github.com/you/RewindEngine
cd RewindEngine
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -e .
```

### Recording

```python
from retrotrace.core.recorder import record, configure
from retrotrace.core.replayer import mock_side_effect

configure(db_path="my_app.db")   # optional; defaults to .retrotrace.db

@record
def calculate_total(items: list, discount: float = 0.0) -> float:
    return sum(i["price"] * i["qty"] for i in items) * (1 - discount)

@mock_side_effect
def charge_card(account_id: str, amount: float) -> dict:
    return payment_gateway.charge(account_id, amount)   # real API call

@record
def process_order(payload: dict) -> dict:
    total = calculate_total(payload["items"], payload.get("discount", 0))
    receipt = charge_card(payload["account_id"], total)
    return {"status": "ok", "receipt": receipt}

# Run it — everything is captured automatically
result = process_order({"account_id": "acct-1", "items": [...]})
```

### Replaying

```python
from retrotrace.storage.ledger import ExecutionLedger
from retrotrace.core.replayer import ReplayEngine

ledger = ExecutionLedger(db_path="my_app.db")
engine = ReplayEngine(ledger=ledger)

# Find the trace you want to replay
traces = ledger.list_traces()
trace_id = traces[-1]["trace_id"]

# Replay — charge_card() body is SKIPPED, original output returned
result = engine.replay_trace(trace_id, process_order, payload)

# Replay async functions
result = await engine.replay_trace_async(trace_id, async_process_order, payload)
```

---

## Run the Example

```bash
python examples/order_service.py
```

```
────────────────────────── Demo 1 — Successful Order ──────────────────────────

  >> PAYMENT GATEWAY CALLED account='acct-42'  amount=$177.73
  >> INVENTORY RESERVED sku='SKU-001'  qty=2  remaining=48
  >> INVENTORY RESERVED sku='SKU-002'  qty=3  remaining=9

┌─────────────────────────────── Order Result ────────────────────────────────┐
│ Order confirmed!                                                             │
│   subtotal  : $197.48                                                       │
│   discount  : -10% ($19.75)                                                 │
│   total     : $177.73                                                       │
└──────────────────────────────────────────────────────────────────────────────┘

────────────────── Demo 3 — Deterministic Replay of Failed Trace ──────────────

  >> @mock_side_effect functions SKIPPED — payment gateway NOT called
  >> Original ValueError reproduced deterministically

┌─────────────────────────────── Replay Result ───────────────────────────────┐
│ Replay reproduced the original error:                                       │
│ Replaying recorded error in 'process_order': ValueError: Unknown discount   │
│ code: 'BOGUS'                                                               │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## CLI Reference

After `pip install -e .`, the `retrotrace` command is available system-wide.

### `retrotrace list`

```
$ retrotrace --db my_app.db list

╭───────────────────────────────── RetroTrace — Recorded Traces ──────────────────────────────────╮
│                                                                                                  │
│   Status  Trace ID                              Root Function   Started At           Events  Error│
│   ──────────────────────────────────────────────────────────────────────────────────────────────│
│   ✓       a97bd6bb-3928-453d-8e06-9abdf5ed0398  process_order  2026-09-19 16:33:12      5        │
│   ✗       76a9d5c7-aae5-4f7c-83c5-ccd3c0148115  process_order  2026-09-19 16:33:12      2    Yes │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### `retrotrace inspect <trace_id>`

```
$ retrotrace --db my_app.db inspect a97bd6bb-3928-453d-8e06-9abdf5ed0398

╭──────────────────────────── Trace: a97bd6bb-… ─────────────────────────────╮
│                                                                              │
│  process_order  [4.82 ms]                                                   │
│    module: examples.order_service                                            │
│    inputs: {"order_payload": {"account_id": "acct-42", ...}}                │
│    return: {"status": "confirmed", ...}                                      │
│  │                                                                           │
│  ├─ calculate_total  [0.10 ms]                                               │
│  │    inputs: {"items": [...], "discount_code": "SAVE10"}                   │
│  │    return: {"subtotal": 197.48, "total": 177.73, ...}                    │
│  │                                                                           │
│  ├─ call_payment_gateway  [52.3 ms]                                          │
│  │    inputs: {"account_id": "acct-42", "amount": 177.73}                   │
│  │    return: {"transaction_id": "3b4a945c-…", "status": "charged"}         │
│  │                                                                           │
│  └─ reserve_inventory  [0.08 ms]                                             │
│       inputs: {"sku": "SKU-001", "quantity": 2}                              │
│       return: {"sku": "SKU-001", "reserved": 2, "remaining": 48}             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### `retrotrace diff <trace_id_1> <trace_id_2>`

```
$ retrotrace --db my_app.db diff a97bd6bb 76a9d5c7

╭───────── RetroTrace — Trace Diff ──────────────────────────────────────────────╮
│  #   Trace A [a97bd6bb…]           Match   Trace B [76a9d5c7…]                 │
│ ─────────────────────────────────────────────────────────────────────────────── │
│  1   process_order  [4.82 ms]        ✓     process_order  [0.08 ms]            │
│      in:  {"order_payload": ...}           in:  {"order_payload": ...}         │
│  2   calculate_total  [0.10 ms]      ~     calculate_total  [0.05 ms]          │
│      in:  {"discount_code": "SAVE10"}      in:  {"discount_code": "BOGUS"}     │
╰────────────────────────────────────────────────────────────────────────────────╯

⚠  Differences detected between the two traces.
```

**Match icons:**

| Icon | Meaning |
|---|---|
| `✓` | Identical function call, identical inputs and outputs |
| `~` | Same function name, but inputs or outputs diverged |
| `✗` | Different function called at this position |
| `±` | One trace has more events than the other |

---

## API Reference

### `@record`

```python
from retrotrace.core.recorder import record

@record                        # bare decorator
@record(ledger=my_ledger)      # explicit ledger
async def my_async_fn(...): ...
```

Captures: `function_name`, `module_path`, `inputs` (bound + defaults), `output`, `error` (full traceback), `started_at`, `completed_at`, `duration_ms`, `trace_id`, `parent_id`.

### `@mock_side_effect`

```python
from retrotrace.core.replayer import mock_side_effect

@mock_side_effect                   # bare
@mock_side_effect(ledger=my_ledger) # explicit ledger
def charge_card(account_id, amount): ...
```

Behaves identically to `@record` during live execution. During replay, the function body is **completely skipped** and the previously recorded return value is returned in its place.

### `ExecutionLedger`

```python
from retrotrace.storage.ledger import ExecutionLedger

ledger = ExecutionLedger(db_path=".retrotrace.db")

ledger.record_event(event)                          # append
events = ledger.get_trace(trace_id)                 # chronological list
summaries = ledger.list_traces()                    # one dict per trace

# Context manager
with ExecutionLedger() as ledger:
    ...
```

### `ReplayEngine`

```python
from retrotrace.core.replayer import ReplayEngine, ExecutionDriftError, TraceNotFoundError

engine = ReplayEngine(ledger=ledger)

# Synchronous
result = engine.replay_trace(trace_id, root_fn, *args, **kwargs)

# Asynchronous
result = await engine.replay_trace_async(trace_id, root_fn, *args, **kwargs)
```

**Raises:**

- `TraceNotFoundError` — `trace_id` not in ledger.
- `ExecutionDriftError` — function sequence or arguments don't match.
- `RuntimeError` — wraps the original exception if the trace ended in error.

---

## Systems Design / FAQ

### How are side effects sandboxed during replay?

`@mock_side_effect` stamps a `_retrotrace_side_effect` attribute on the raw function at decoration time. When `ReplayEngine.consume()` intercepts a call during replay it checks this attribute. If found, it skips the function body entirely and returns the recorded output from the ledger. No network request, database write, or email is sent.

The interception works via two [`contextvars.ContextVar`](https://docs.python.org/3/library/contextvars.html) objects injected into `recorder.py`:

```python
is_replaying:  ContextVar[bool]   # True during replay_trace()
replay_engine: ContextVar[Any]    # reference to the active ReplayEngine
```

Every `@record` / `@mock_side_effect` wrapper checks `is_replaying` first. If active it calls `engine.consume()` instead of executing live. Context vars are reset in a `finally` block after each `replay_trace()` call, so regular execution resumes immediately afterwards.

### How are non-serialisable objects handled?

`_safe_serialize(value)` in `recorder.py` applies a three-layer fallback:

1. `json.dumps(value)` — succeeds for primitives, dicts, lists, numbers, booleans, `None`.
2. Element-wise recursion for `dict` / `list` / `tuple`.
3. `repr(value)` — the ultimate fallback; always produces a string.

This means recording **never crashes the host application**, even if you pass a SQLAlchemy model, a numpy array, or a lambda.

### How is the nested call stack preserved?

Two `ContextVar` objects thread- and async-safely propagate through every frame:

- `current_trace_id` — one UUID per root call, shared by all descendants.
- `current_parent_id` — set to the current event's `event_id` before calling children, then reset to the previous value in `finally`.

This produces a complete call tree without any manual instrumentation. The ledger stores `parent_id` on every event, and `_build_tree()` in the CLI reconstructs the hierarchy at display time.

### How does RetroTrace differ from traditional logging / Sentry / OpenTelemetry?

| | RetroTrace | Sentry / Rollbar | OpenTelemetry |
|---|---|---|---|
| **Primary purpose** | Deterministic replay | Error reporting | Distributed tracing |
| **Captures return values** | Yes — every call | No | No |
| **Captures full input bindings** | Yes — with defaults | Manually added | No |
| **Replay without side effects** | Yes — `@mock_side_effect` | No | No |
| **Drift detection** | Yes — `ExecutionDriftError` | No | No |
| **Storage** | Local SQLite file | Cloud (Sentry SaaS) | Exporter-dependent |
| **Overhead** | Per-call (sync/async) | Exception-only | Span-level |
| **Setup** | One decorator | SDK + DSN config | Extensive config |

RetroTrace is not a replacement for production observability tools — it is a **developer-time** instrument for achieving deterministic reproducibility of function-level bugs.

---

## Project Structure

```
RewindEngine/
├── retrotrace/
│   ├── storage/
│   │   └── ledger.py          # ExecutionEvent model + ExecutionLedger (SQLite)
│   ├── core/
│   │   ├── recorder.py        # @record decorator, context vars, serialisation
│   │   └── replayer.py        # ReplayEngine, @mock_side_effect, custom errors
│   └── cli/
│       └── main.py            # Rich TUI: list / inspect / diff commands
├── tests/
│   ├── test_ledger.py         # 23 tests
│   ├── test_recorder.py       # 24 tests
│   ├── test_replayer.py       # 19 tests
│   └── test_cli.py            # 32 tests
├── examples/
│   └── order_service.py       # End-to-end order pipeline showcase
├── pyproject.toml
└── README.md
```

---

## Running the Test Suite

```bash
pytest tests/ -v
```

```
98 passed in 2.05s
```

All 98 tests pass across storage, recorder, replayer, and CLI layers.

---

## License

MIT © 2026 Viswajith
