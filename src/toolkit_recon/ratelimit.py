"""A FIFO sliding-window token governor for the LLM endpoint.

The extraction model enforces a tokens-per-minute quota (8,000 TPM on the tier
this was built against, reported via `x-ratelimit-limit-tokens`). Eight
concurrent workers will blow through that instantly, and the failure mode is a
429 storm that retries cannot rescue — every retry also costs quota. So we
pace *before* spending: each call reserves its estimated cost and waits until
the trailing 60-second window has room.

This module has been wrong twice, in opposite directions, and the current
design is shaped by both failures:

1. **Starvation.** The first version released its lock while sleeping. Every
   blocked worker woke on the same window roll, raced for one slot, and the
   losers slept another full window. One app waited 4,063 seconds to do 3,216
   tokens of work.

2. **Unbounded blocking.** The fix held the lock *across* the sleep, which
   made admission FIFO but meant one waiter could hold every other caller
   behind it indefinitely, with no upper bound and nothing logged.

The design below keeps FIFO without either hazard. A waiter takes a ticket and
joins a queue; the lock is taken only to inspect state and is **never held
across an await**; and only the queue head may be admitted, which is what makes
the ordering fair. Every wait is bounded by `max_wait`, after which `acquire`
raises rather than blocking forever — a loud, retryable failure beats a silent
stall.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from .throttle import RetryableError


def estimate_tokens(text: str) -> int:
    """Rough char->token estimate. Deliberately pessimistic.

    ~3.4 chars/token is conservative for English prose with markdown; over-
    estimating costs a little throughput, under-estimating costs a 429.
    """
    return int(len(text) / 3.4) + 32


@dataclass
class _Ticket:
    tokens: int
    admitted: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)


class TokenRateLimiter:
    def __init__(
        self,
        tokens_per_minute: int = 8000,
        window: float = 60.0,
        headroom: float = 0.95,
        max_wait: float = 300.0,
    ) -> None:
        # Headroom is a throughput knob, not just a safety margin. A measured
        # extraction costs ~3.6k tokens; at 0.80 only one fits per 8k window
        # (sawtooth, one app/minute) while at 0.95 two do, halving the run.
        # Overshoot is still caught by the 429 handler and `penalise`.
        self.capacity = max(1, int(tokens_per_minute * headroom))
        self.window = window
        self.max_wait = max_wait
        self._spent: deque[tuple[float, int]] = deque()
        self._queue: deque[_Ticket] = deque()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        self.waits = 0
        self.wait_seconds = 0.0
        self.timeouts = 0
        self.abandoned = 0

    # -- state helpers; all callers hold the lock ---------------------------

    def _prune(self, now: float) -> None:
        while self._spent and now - self._spent[0][0] > self.window:
            self._spent.popleft()

    def _in_window(self) -> int:
        return sum(t for _, t in self._spent)

    def _admit_head(self) -> None:
        """Admit as many queue-head tickets as fit. FIFO: only ever the head.

        Serving strictly from the front is what prevents starvation — a small
        request cannot jump a large one that has been waiting longer.
        """
        while self._queue:
            now = time.monotonic()
            self._prune(now)
            if now < self._blocked_until:
                return
            head = self._queue[0]
            if self._in_window() + head.tokens > self.capacity:
                return
            self._queue.popleft()
            self._spent.append((now, head.tokens))
            head.admitted = True
            head.event.set()

    def _refund(self, tokens: int) -> None:
        """Give back reserved quota, newest entries first."""
        refund = tokens
        for i in range(len(self._spent) - 1, -1, -1):
            if refund <= 0:
                break
            ts, amt = self._spent[i]
            take = min(amt, refund)
            self._spent[i] = (ts, amt - take)
            refund -= take

    def _release(self, ticket: _Ticket) -> None:
        """Drop a ticket whose caller has gone away.

        This is the fix for the bug that wedged three concurrent extractions
        with 6,691 of 7,600 tokens reserved and nothing running. A cancelled
        or timed-out `acquire` used to leave its ticket in the queue; the
        scheduler would then admit that ghost, spend its quota on a request
        nobody was going to make, and every real waiter starved behind it. The
        leak was permanent and compounding, so the run degraded to a stall.

        Synchronous on purpose: it must run inside `finally`, including during
        cancellation, where awaiting a lock is not safe. Nothing here awaits,
        so the event loop cannot interleave another coroutine mid-update.
        """
        if ticket.admitted:
            self._refund(ticket.tokens)  # won a slot, caller gone: hand it back
            self.abandoned += 1
            return
        try:
            self._queue.remove(ticket)
            self.abandoned += 1
        except ValueError:
            pass

    def _delay_hint(self) -> float:
        """How long until state could plausibly change. Never held under await."""
        now = time.monotonic()
        if now < self._blocked_until:
            return self._blocked_until - now
        if self._spent:
            return max(0.05, self.window - (now - self._spent[0][0]) + 0.01)
        return 0.05

    # -- public API --------------------------------------------------------

    async def acquire(self, tokens: int) -> None:
        """Reserve `tokens`, waiting for room. FIFO, bounded, never deadlocks.

        Raises RetryableError if `max_wait` elapses, so the caller's backoff
        can deal with it instead of the run hanging with nothing logged.
        """
        tokens = min(tokens, self.capacity)  # a huge single call must still pass
        ticket = _Ticket(tokens)
        deadline = time.monotonic() + self.max_wait

        async with self._lock:
            self._queue.append(ticket)
            self._admit_head()
        if ticket.admitted:
            return

        # From here the ticket is live in the queue, so every exit path --
        # return, raise, or cancellation -- must account for it. Leaking one
        # leaks quota forever; see _release.
        admitted = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if ticket.admitted:
                        admitted = True
                        return
                    self.timeouts += 1
                    raise RetryableError(
                        f"token limiter: waited {self.max_wait:.0f}s for "
                        f"{tokens} tokens without admission"
                    )

                async with self._lock:
                    hint = self._delay_hint()
                # Cap the nap so a waiter always rechecks periodically, even
                # if a wake-up is missed. Correctness never depends on the hint.
                nap = max(0.05, min(hint, 5.0, remaining))
                self.waits += 1
                self.wait_seconds += nap
                try:
                    await asyncio.wait_for(ticket.event.wait(), timeout=nap)
                except (TimeoutError, asyncio.TimeoutError):
                    pass

                if ticket.admitted:
                    admitted = True
                    return
                async with self._lock:
                    self._admit_head()
                if ticket.admitted:
                    admitted = True
                    return
        finally:
            if not admitted:
                self._release(ticket)

    def settle(self, estimated: int, actual: int) -> None:
        """Reconcile the reservation against the usage the API reported.

        Corrects in both directions. Refunds matter: the completion cap is a
        safety limit, and reserving it in full would idle most of the quota
        waiting on tokens no request actually spends.
        """
        if actual <= 0:
            return
        delta = actual - min(estimated, self.capacity)
        if delta > 0:
            self._spent.append((time.monotonic(), delta))
            return
        refund = -delta
        for i in range(len(self._spent) - 1, -1, -1):
            if refund <= 0:
                break
            ts, amt = self._spent[i]
            take = min(amt, refund)
            self._spent[i] = (ts, amt - take)
            refund -= take

    def penalise(self, seconds: float) -> None:
        """Park every caller, e.g. after a 429 with a Retry-After header."""
        self._blocked_until = max(
            self._blocked_until, time.monotonic() + max(0.0, seconds)
        )

    def stats(self) -> dict:
        return {
            "capacity": self.capacity,
            "in_window": self._in_window(),
            "queued": len(self._queue),
            "waits": self.waits,
            "wait_seconds": round(self.wait_seconds, 1),
            "timeouts": self.timeouts,
            "abandoned": self.abandoned,
        }
