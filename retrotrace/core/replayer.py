"""
retrotrace/core/replayer.py
Deterministic replay engine for RetroTrace.

Architecture
------------
``ReplayEngine.replay_trace()`` loads a previously recorded trace from the
``ExecutionLedger``, sets the ``is_replaying`` / ``replay_engine`` context
vars, then re-runs the root function.  As each ``@record``-decorated frame
is entered during that re-run, ``ReplayEngine.consume()`` is called instead
of live recording.  It:

1. Pops the next expected event from a FIFO queue (ordered by ``started_at``).
2. Validates that the function name and inputs match — raising
   ``ExecutionDriftError`` on mismatch.
3. If the expected event has ``is_side_effect=True`` (i.e. was marked with
   ``@mock_side_effect``), the function body is skipped and the recorded
   output is returned immediately.
4. Otherwise the function body executes normally (re-running pure logic).
5. If the original event ended in an error, that error is re-raised as a
   ``RuntimeError`` after the queue is exhausted.

``@mock_side_effect`` wraps external boundaries.  During live execution it
records normally (same as ``@record``).  During replay it tags its event so
``consume()`` knows to skip the body.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
from collections import deque
from typing import Any, Callable, Deque, List, Optional

from retrotrace.storage.ledger import ExecutionEvent, ExecutionLedger
from retrotrace.core.recorder import (
    _capture_inputs,
    _safe_serialize,
    _sync_wrapper,
    _async_wrapper,
    is_replaying,
    replay_engine,
    record,
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class TraceNotFoundError(Exception):
    """Raised when a requested trace_id does not exist in the ledger."""


class ExecutionDriftError(Exception):
    """
    Raised when arguments or the call sequence during replay diverge from
    the originally recorded trace.
    """


# ---------------------------------------------------------------------------
# Side-effect marker
# ---------------------------------------------------------------------------

#: Internal attribute stamped onto wrapper functions by @mock_side_effect.
_SIDE_EFFECT_ATTR = "_retrotrace_side_effect"


def mock_side_effect(
    fn: Optional[Callable] = None,
    *,
    ledger: Optional[ExecutionLedger] = None,
) -> Any:
    """
    Decorator for external boundaries (network, DB, payment gateways …).

    * **Live execution**: records the call and real result exactly like
      ``@record``.
    * **Replay**: skips the function body and returns the previously recorded
      output directly, preventing real side effects from firing.

    Usage::

        @mock_side_effect
        def charge_card(amount: float) -> str:
            return payment_gateway.charge(amount)
    """
    def decorator(func: Callable) -> Callable:
        # Stamp the raw function BEFORE wrapping so consume() — which receives
        # the unwrapped fn — can detect side-effect status via _is_side_effect().
        setattr(func, _SIDE_EFFECT_ATTR, True)
        if inspect.iscoroutinefunction(func):
            wrapper = _async_wrapper(func, ledger)
        else:
            wrapper = _sync_wrapper(func, ledger)
        # Also stamp the wrapper itself (for introspection / completeness).
        setattr(wrapper, _SIDE_EFFECT_ATTR, True)
        return wrapper

    if fn is not None:
        return decorator(fn)
    return decorator


def _is_side_effect(fn: Callable) -> bool:
    return bool(getattr(fn, _SIDE_EFFECT_ATTR, False))


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------

class ReplayEngine:
    """
    Deterministic replay engine.

    Parameters
    ----------
    ledger:
        The ``ExecutionLedger`` that holds the recorded traces to replay.
    """

    def __init__(self, ledger: ExecutionLedger) -> None:
        self._ledger = ledger
        # Active event queue populated at the start of each replay_trace() call.
        self._queue: Deque[ExecutionEvent] = deque()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def replay_trace(
        self,
        trace_id: str,
        target_fn: Callable,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Re-execute *target_fn* deterministically against a previously recorded
        trace.

        Parameters
        ----------
        trace_id:
            The UUID of the trace to replay.
        target_fn:
            The root callable to invoke (must be decorated with ``@record`` or
            ``@mock_side_effect`` for its nested calls to be intercepted).
        *args / **kwargs:
            Arguments forwarded to *target_fn*.

        Returns
        -------
        Any
            The return value from the replay run.

        Raises
        ------
        TraceNotFoundError
            If *trace_id* is not found in the ledger.
        ExecutionDriftError
            If any function call's arguments deviate from the recording.
        RuntimeError
            If the original trace ended in an unhandled exception, that error
            is reproduced deterministically.
        """
        events = self._ledger.get_trace(trace_id)
        if not events:
            raise TraceNotFoundError(
                f"No events found for trace_id={trace_id!r}"
            )

        self._queue = deque(events)

        tok_replaying = is_replaying.set(True)
        tok_engine = replay_engine.set(self)
        try:
            if inspect.iscoroutinefunction(target_fn):
                # Run the coroutine synchronously inside replay_trace so callers
                # don't need to await it.  For async tests use replay_trace_async.
                return asyncio.get_event_loop().run_until_complete(
                    target_fn(*args, **kwargs)
                )
            return target_fn(*args, **kwargs)
        finally:
            is_replaying.reset(tok_replaying)
            replay_engine.reset(tok_engine)

    async def replay_trace_async(
        self,
        trace_id: str,
        target_fn: Callable,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Async variant of :meth:`replay_trace` for use inside ``async def`` test
        functions or async frameworks.
        """
        events = self._ledger.get_trace(trace_id)
        if not events:
            raise TraceNotFoundError(
                f"No events found for trace_id={trace_id!r}"
            )

        self._queue = deque(events)

        tok_replaying = is_replaying.set(True)
        tok_engine = replay_engine.set(self)
        try:
            if inspect.iscoroutinefunction(target_fn):
                return await target_fn(*args, **kwargs)
            return target_fn(*args, **kwargs)
        finally:
            is_replaying.reset(tok_replaying)
            replay_engine.reset(tok_engine)

    # ------------------------------------------------------------------
    # Internal: called back from @record / @mock_side_effect wrappers
    # ------------------------------------------------------------------

    def consume(
        self,
        fn: Callable,
        args: tuple,
        kwargs: dict,
        *,
        is_async: bool,
    ) -> Any:
        """
        Match *fn* against the next expected event in the queue, validate
        inputs, and either run the function or return the recorded output.

        Called automatically by the patched ``_sync_wrapper`` /
        ``_async_wrapper`` when ``is_replaying`` is active.
        """
        if not self._queue:
            raise ExecutionDriftError(
                f"Unexpected call to {fn.__name__!r}: event queue is empty"
            )

        expected = self._queue.popleft()

        # ── 1. Function-name check ────────────────────────────────────────────
        if expected.function_name != fn.__name__:
            raise ExecutionDriftError(
                f"Call sequence drift: expected {expected.function_name!r}, "
                f"got {fn.__name__!r}"
            )

        # ── 2. Input drift check ──────────────────────────────────────────────
        actual_inputs = _capture_inputs(fn, args, kwargs)
        if actual_inputs != expected.inputs:
            raise ExecutionDriftError(
                f"Argument drift in {fn.__name__!r}:\n"
                f"  expected: {expected.inputs}\n"
                f"  actual  : {actual_inputs}"
            )

        # ── 3. If original trace ended in error, reproduce it ─────────────────
        if expected.error is not None:
            # Extract the first line of the error string for a clean message.
            first_line = expected.error.strip().splitlines()[-1]
            raise RuntimeError(
                f"Replaying recorded error in {fn.__name__!r}: {first_line}"
            )

        # ── 4. Side-effect boundary: skip body, return recorded output ─────────
        if _is_side_effect(fn):
            return _make_return(expected.output, is_async=is_async)

        # ── 5. Pure / intermediate function: run body normally ─────────────────
        if is_async:
            return _run_async_live(fn, args, kwargs)
        return fn(*args, **kwargs)

    # ------------------------------------------------------------------
    # list_traces convenience pass-through
    # ------------------------------------------------------------------

    def list_traces(self):
        return self._ledger.list_traces()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_return(recorded_output: Any, *, is_async: bool) -> Any:
    """
    Wrap *recorded_output* in a coroutine when the caller expects to ``await``.
    """
    if is_async:
        async def _coro():
            return recorded_output
        return _coro()
    return recorded_output


async def _run_async_live(fn: Callable, args: tuple, kwargs: dict) -> Any:
    """Await an async function body during replay (pure / non-side-effect)."""
    return await fn(*args, **kwargs)
