"""
retrotrace/core/recorder.py
Execution recorder: the ``@record`` decorator that intercepts sync and async
function calls and appends an :class:`ExecutionEvent` to an
:class:`ExecutionLedger`.

Context tracking
----------------
Two :class:`contextvars.ContextVar` objects thread-safely propagate trace
state across nested (and async) call stacks:

* ``current_trace_id``  – UUID shared by every event in one "run"
* ``current_parent_id`` – ``event_id`` of the immediately enclosing call
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import time
import traceback
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, TypeVar, overload

from retrotrace.storage.ledger import ExecutionEvent, ExecutionLedger

# ---------------------------------------------------------------------------
# Context variables
# ---------------------------------------------------------------------------

current_trace_id: ContextVar[Optional[str]] = ContextVar(
    "current_trace_id", default=None
)
current_parent_id: ContextVar[Optional[str]] = ContextVar(
    "current_parent_id", default=None
)

# ---------------------------------------------------------------------------
# Singleton / configurable default ledger
# ---------------------------------------------------------------------------

_default_ledger: Optional[ExecutionLedger] = None


def configure(db_path: str = ".retrotrace.db") -> ExecutionLedger:
    """
    Point the global default ledger at *db_path* and return it.

    Call this once at application startup:

    .. code-block:: python

        from retrotrace.core.recorder import configure
        configure(db_path="my_app.db")
    """
    global _default_ledger
    _default_ledger = ExecutionLedger(db_path=db_path)
    return _default_ledger


def _get_default_ledger() -> ExecutionLedger:
    global _default_ledger
    if _default_ledger is None:
        _default_ledger = ExecutionLedger()
    return _default_ledger


# ---------------------------------------------------------------------------
# Safe serialisation helpers
# ---------------------------------------------------------------------------

def _safe_serialize(value: Any) -> Any:
    """
    Return a JSON-serialisable representation of *value*.

    * If *value* is already JSON-serialisable, return it as-is.
    * Otherwise fall back to ``repr()``.
    * Never raises.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        pass
    # Try converting dicts/lists element-by-element
    if isinstance(value, dict):
        return {str(k): _safe_serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_serialize(v) for v in value]
    return repr(value)


def _capture_inputs(fn: Callable, args: tuple, kwargs: dict) -> Dict[str, Any]:
    """
    Bind *args* / *kwargs* to *fn*'s signature (including defaults) and
    return a JSON-safe mapping of parameter-name → value.
    """
    try:
        sig = inspect.signature(fn)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return {k: _safe_serialize(v) for k, v in bound.arguments.items()}
    except Exception:  # pragma: no cover – defensive
        return {"_args": _safe_serialize(args), "_kwargs": _safe_serialize(kwargs)}


# ---------------------------------------------------------------------------
# Core event builder
# ---------------------------------------------------------------------------

def _build_event(
    *,
    event_id: str,
    trace_id: str,
    parent_id: Optional[str],
    fn: Callable,
    inputs: Dict[str, Any],
    output: Any,
    error: Optional[str],
    started_at: datetime,
    completed_at: datetime,
    duration_ms: float,
) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=event_id,
        trace_id=trace_id,
        parent_id=parent_id,
        function_name=fn.__name__,
        module_path=fn.__module__ or "",
        inputs=inputs,
        output=_safe_serialize(output) if output is not None else None,
        error=error,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# The @record decorator
# ---------------------------------------------------------------------------

F = TypeVar("F", bound=Callable)


def record(
    fn: Optional[F] = None,
    *,
    ledger: Optional[ExecutionLedger] = None,
) -> Any:
    """
    Decorator that records every call to a sync or async function.

    Usage::

        @record
        def my_func(x, y):
            return x + y

        # — or with a custom ledger —
        @record(ledger=my_ledger)
        async def my_async_func(x):
            ...

    Parameters
    ----------
    fn:
        The function being decorated.  When ``@record`` is used *without*
        parentheses this is the function itself; with parentheses it is
        ``None`` and the real function arrives via the returned wrapper.
    ledger:
        An explicit :class:`ExecutionLedger` to write to.  When omitted the
        global default ledger (see :func:`configure`) is used.
    """
    # Support both @record and @record(ledger=...) usage
    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):
            return _async_wrapper(func, ledger)  # type: ignore[return-value]
        return _sync_wrapper(func, ledger)  # type: ignore[return-value]

    if fn is not None:
        # Called as @record (no parentheses)
        return decorator(fn)
    # Called as @record(...) with keyword args
    return decorator


def _sync_wrapper(fn: Callable, explicit_ledger: Optional[ExecutionLedger]) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _ledger = explicit_ledger or _get_default_ledger()
        event_id = str(uuid.uuid4())
        trace_id = current_trace_id.get() or str(uuid.uuid4())
        parent_id = current_parent_id.get()
        inputs = _capture_inputs(fn, args, kwargs)

        # Propagate context to any callees
        tok_trace = current_trace_id.set(trace_id)
        tok_parent = current_parent_id.set(event_id)

        started_at = datetime.now(tz=timezone.utc)
        t0 = time.monotonic()
        output: Any = None
        error: Optional[str] = None

        try:
            output = fn(*args, **kwargs)
            return output
        except Exception as exc:
            error = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            raise
        finally:
            completed_at = datetime.now(tz=timezone.utc)
            duration_ms = (time.monotonic() - t0) * 1000

            # Restore context regardless of success/failure
            current_trace_id.reset(tok_trace)
            current_parent_id.reset(tok_parent)

            event = _build_event(
                event_id=event_id,
                trace_id=trace_id,
                parent_id=parent_id,
                fn=fn,
                inputs=inputs,
                output=output,
                error=error,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
            )
            _ledger.record_event(event)

    return wrapper


def _async_wrapper(fn: Callable, explicit_ledger: Optional[ExecutionLedger]) -> Callable:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        _ledger = explicit_ledger or _get_default_ledger()
        event_id = str(uuid.uuid4())
        trace_id = current_trace_id.get() or str(uuid.uuid4())
        parent_id = current_parent_id.get()
        inputs = _capture_inputs(fn, args, kwargs)

        tok_trace = current_trace_id.set(trace_id)
        tok_parent = current_parent_id.set(event_id)

        started_at = datetime.now(tz=timezone.utc)
        t0 = time.monotonic()
        output: Any = None
        error: Optional[str] = None

        try:
            output = await fn(*args, **kwargs)
            return output
        except Exception as exc:
            error = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            raise
        finally:
            completed_at = datetime.now(tz=timezone.utc)
            duration_ms = (time.monotonic() - t0) * 1000

            current_trace_id.reset(tok_trace)
            current_parent_id.reset(tok_parent)

            event = _build_event(
                event_id=event_id,
                trace_id=trace_id,
                parent_id=parent_id,
                fn=fn,
                inputs=inputs,
                output=output,
                error=error,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
            )
            _ledger.record_event(event)

    return wrapper
