"""Tests for the token governor and relevance-based condensation.

These two carry the run: without them the extraction endpoint 429s on the very
first app, and without condensation the token budget would be spent on
navigation chrome instead of auth sections.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toolkit_recon.condense import condense, score_block  # noqa: E402
from toolkit_recon.ratelimit import TokenRateLimiter, estimate_tokens  # noqa: E402


# ---------------- condensation ----------------

FILLER = (
    "Welcome to the product. Our mission is to help teams collaborate.\n\n"
    "Follow us on social media for updates and company news.\n\n"
) * 12

AUTH = (
    "## Authentication\n\n"
    "All requests use OAuth2. Send an access token in the Authorization "
    "header as a Bearer token. You may also use an API key for server-side "
    "calls. Scopes are required per endpoint.\n\n"
)

PRICING = (
    "## Pricing\n\n"
    "API access requires the Enterprise plan. The free tier does not include "
    "API access; a trial is available on request.\n\n"
)


def test_condense_keeps_auth_over_filler():
    doc = FILLER + AUTH + FILLER
    out = condense(doc, 900)
    assert len(out) <= 900
    assert "OAuth2" in out and "Bearer" in out


def test_condense_keeps_both_auth_and_pricing_when_budget_allows():
    doc = FILLER + AUTH + FILLER + PRICING + FILLER
    out = condense(doc, 1600)
    assert "OAuth2" in out
    assert "Enterprise plan" in out


def test_condense_beats_naive_truncation():
    # The exact failure mode this module exists to prevent: the evidence sits
    # past the point a blind [:n] slice would cut.
    doc = FILLER + AUTH
    assert "OAuth2" not in doc[:900]
    assert "OAuth2" in condense(doc, 900)


def test_condense_is_a_noop_under_budget():
    assert condense("short doc", 5000) == "short doc"


def test_condense_marks_omissions():
    doc = FILLER + AUTH + FILLER + PRICING + FILLER
    assert "[... omitted ...]" in condense(doc, 1600)


def test_condense_respects_budget_on_pathological_input():
    doc = ("authentication oauth api key bearer token " * 400)
    assert len(condense(doc, 1000)) <= 1000


def test_mcp_scores_above_generic_prose():
    assert score_block("The official MCP server is available.") > score_block(
        "We are hiring across engineering."
    )


# ---------------- token governor ----------------


def test_estimate_is_conservative():
    # Must not under-estimate; over-estimating only costs throughput.
    assert estimate_tokens("a" * 3400) >= 1000


def test_limiter_admits_within_capacity():
    async def go():
        lim = TokenRateLimiter(tokens_per_minute=1000, window=60.0, headroom=1.0)
        t0 = time.monotonic()
        for _ in range(5):
            await lim.acquire(200)
        return time.monotonic() - t0

    assert asyncio.run(go()) < 0.5


def test_limiter_blocks_once_window_is_full():
    async def go():
        lim = TokenRateLimiter(tokens_per_minute=1000, window=1.0, headroom=1.0)
        await lim.acquire(1000)
        t0 = time.monotonic()
        await lim.acquire(500)  # must wait for the window to roll
        return time.monotonic() - t0

    assert asyncio.run(go()) >= 0.5


def test_oversized_request_still_passes():
    async def go():
        lim = TokenRateLimiter(tokens_per_minute=1000, window=1.0, headroom=1.0)
        await asyncio.wait_for(lim.acquire(999_999), timeout=3)
        return True

    assert asyncio.run(go())


def test_penalise_parks_callers():
    async def go():
        lim = TokenRateLimiter(tokens_per_minute=10_000, window=60.0, headroom=1.0)
        lim.penalise(0.6)
        t0 = time.monotonic()
        await lim.acquire(10)
        return time.monotonic() - t0

    assert asyncio.run(go()) >= 0.5


def test_no_starvation_under_contention():
    """Regression: contending workers must be admitted in bounded time.

    The original limiter released its lock while sleeping, so every waiter woke
    on the same window roll, raced for one slot, and the losers slept another
    full window. One app in a five-app run waited 68 minutes. Admission is now
    FIFO, so N workers needing one slot each finish in about N windows.
    """

    async def go():
        # Capacity fits exactly one reservation, so all five must queue.
        lim = TokenRateLimiter(tokens_per_minute=100, window=0.4, headroom=1.0)
        order: list[int] = []

        async def worker(i: int) -> None:
            await lim.acquire(100)
            order.append(i)

        t0 = time.monotonic()
        await asyncio.wait_for(
            asyncio.gather(*(worker(i) for i in range(5))), timeout=10
        )
        return order, time.monotonic() - t0

    order, elapsed = asyncio.run(go())
    assert order == [0, 1, 2, 3, 4], f"admission was not FIFO: {order}"
    # 5 slots x 0.4s window ~= 1.6s; generous ceiling, but far below the
    # unbounded starvation the old implementation allowed.
    assert elapsed < 5.0, f"took {elapsed:.1f}s — starvation regression"


def test_settle_charges_the_difference():
    async def go():
        lim = TokenRateLimiter(tokens_per_minute=1000, window=60.0, headroom=1.0)
        await lim.acquire(100)
        lim.settle(100, 900)  # actually cost 900
        t0 = time.monotonic()
        # 100 reserved + 800 correction = 900 spent; 200 more must not fit.
        task = asyncio.create_task(lim.acquire(200))
        await asyncio.sleep(0.3)
        blocked = not task.done()
        task.cancel()
        return blocked, time.monotonic() - t0

    blocked, _ = asyncio.run(go())
    assert blocked
