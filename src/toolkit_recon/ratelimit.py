"""A sliding-window token governor for the LLM endpoint.

The extraction model enforces a tokens-per-minute quota (8,000 TPM on the tier
this was built against, reported via `x-ratelimit-limit-tokens`). Eight
concurrent workers will blow through that instantly, and the failure mode is a
429 storm that retries cannot rescue — every retry also costs quota.

So we pace *before* spending rather than backing off after: each call reserves
its estimated cost and waits until the trailing 60-second window has room.
Retry-after headers still feed back in via `penalise`, which parks all callers
when the server tells us we got it wrong.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


def estimate_tokens(text: str) -> int:
    """Rough char->token estimate. Deliberately pessimistic.

    ~3.4 chars/token is conservative for English prose with markdown; over-
    estimating costs a little throughput, under-estimating costs a 429.
    """
    return int(len(text) / 3.4) + 32


class TokenRateLimiter:
    def __init__(
        self, tokens_per_minute: int = 8000, window: float = 60.0, headroom: float = 0.95
    ) -> None:
        # Headroom is a throughput knob, not just a safety margin. A measured
        # extraction costs ~3.6k tokens; at 0.80 only one fits per 8k window
        # (sawtooth, one app/minute) while at 0.95 two do, halving the run.
        # Overshoot is still caught by the 429 handler and `penalise`.
        # Headroom absorbs the gap between our estimate and the server's count.
        self.capacity = max(1, int(tokens_per_minute * headroom))
        self.window = window
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        self.waits = 0
        self.wait_seconds = 0.0

    def _prune(self, now: float) -> None:
        while self._spent and now - self._spent[0][0] > self.window:
            self._spent.popleft()

    def _in_window(self) -> int:
        return sum(t for _, t in self._spent)

    async def acquire(self, tokens: int) -> None:
        """Reserve `tokens`, waiting for room. Strictly FIFO.

        The lock is deliberately held *across* the sleep. Releasing it while
        waiting lets every blocked worker wake on the same window roll, race
        for one slot, and send the losers back to sleep for another full
        window — starvation that measured as a 68-minute wait for one app in a
        five-app run. Serialising admission makes the wait bounded and fair:
        `asyncio.Lock` queues waiters in arrival order, so each task is
        admitted as soon as the quota it needs actually exists.
        """
        tokens = min(tokens, self.capacity)  # a huge single call must still pass
        async with self._lock:
            while True:
                now = time.monotonic()
                self._prune(now)

                if now >= self._blocked_until and self._in_window() + tokens <= self.capacity:
                    self._spent.append((now, tokens))
                    return

                if now < self._blocked_until:
                    wait = self._blocked_until - now
                else:
                    # Sleep until the oldest reservation ages out of the window.
                    wait = self.window - (now - self._spent[0][0]) + 0.01

                wait = min(max(wait, 0.05), self.window + 1.0)
                self.waits += 1
                self.wait_seconds += wait
                await asyncio.sleep(wait)

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
        # Refund by shrinking the most recent entries, never below zero.
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
