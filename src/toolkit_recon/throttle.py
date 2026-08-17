"""Per-domain politeness delay and retry-with-backoff helpers."""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from urllib.parse import urlparse

from .config import settings


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return ""


class DomainThrottle:
    """Guarantees at least `delay` seconds between two hits on the same host.

    Concurrency is capped globally by a semaphore, but eight workers can still
    land on docs.stripe.com at once. This serialises per host without
    serialising the whole run.
    """

    def __init__(self, delay: float | None = None) -> None:
        self.delay = settings.domain_delay if delay is None else delay
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = {}

    async def acquire(self, url: str) -> None:
        host = domain_of(url)
        if not host:
            return
        async with self._locks[host]:
            wait = self.delay - (time.monotonic() - self._last.get(host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()


class RetryableError(Exception):
    """429 / 5xx / transport error — worth another go."""


class PermanentError(Exception):
    """404 and friends — retrying just wastes everyone's time."""


async def with_backoff(fn, *, attempts: int | None = None, base: float = 1.5):
    """Call an async `fn`, retrying only on RetryableError.

    Exponential backoff with full jitter, so eight workers that all get 429'd
    at the same moment do not all come back at the same moment.
    """
    attempts = attempts or settings.max_retries
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except PermanentError:
            raise
        except RetryableError as e:
            last = e
            if i == attempts - 1:
                break
            sleep = min(base ** (i + 1), 30.0)
            await asyncio.sleep(random.uniform(0, sleep))
    raise last if last else RuntimeError("retry loop exhausted with no error")
