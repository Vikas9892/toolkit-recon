"""Search + fetch providers.

Primary is Composio: `COMPOSIO_SEARCH_WEB` for search and
`COMPOSIO_SEARCH_FETCH_URL_CONTENT` for clean page text. Both are
Composio-managed (`no_auth: true`), so the project key is the only credential
and no per-app OAuth is involved.

Tool slugs and toolkit versions are *discovered at runtime* via
`get_raw_composio_tool_by_slug` and then pinned for the whole run, so a
mid-run toolkit release cannot silently change the extraction inputs.

A keyless DirectProvider is kept as a fallback so the pipeline still runs (and
the test suite still passes) on a machine with no Composio key.
"""

from __future__ import annotations

import asyncio
import re
from typing import Protocol

import httpx

from .cache import DiskCache, key_for
from .config import settings
from .schema import FetchedDoc, SearchHit
from .throttle import (
    DomainThrottle,
    PermanentError,
    RetryableError,
    with_backoff,
)

_STATUS_RE = re.compile(r"\b(4\d\d|5\d\d)\b")


def _classify(msg: str) -> Exception:
    """Map a provider error string onto retryable vs permanent."""
    m = _STATUS_RE.search(msg or "")
    if m:
        code = int(m.group(1))
        if code == 429 or code >= 500:
            return RetryableError(msg)
        return PermanentError(msg)
    if any(w in (msg or "").lower() for w in ("timeout", "timed out", "connection", "reset")):
        return RetryableError(msg)
    return PermanentError(msg)


class Provider(Protocol):
    name: str

    async def search(self, query: str) -> list[SearchHit]: ...
    async def fetch(self, url: str) -> FetchedDoc: ...


# ---------------------------------------------------------------------------
# Composio
# ---------------------------------------------------------------------------

SEARCH_TOOL = "COMPOSIO_SEARCH_WEB"
FETCH_TOOL = "COMPOSIO_SEARCH_FETCH_URL_CONTENT"


class ComposioProvider:
    name = "composio"

    def __init__(self) -> None:
        from composio import Composio  # imported lazily: optional at test time

        self._client = Composio()  # reads COMPOSIO_API_KEY from the environment
        self._user_id = settings.composio_user_id
        self._versions: dict[str, str] = {}
        self.search_cache = DiskCache("composio_search")
        self.fetch_cache = DiskCache("composio_fetch")
        self.throttle = DomainThrottle()
        self.request_ids: list[str] = []

    # -- version discovery -------------------------------------------------

    async def prepare(self) -> dict[str, str]:
        """Resolve and pin the current toolkit version for each tool we use."""
        for slug in (SEARCH_TOOL, FETCH_TOOL):
            tool = await asyncio.to_thread(
                self._client.tools.get_raw_composio_tool_by_slug, slug
            )
            self._versions[slug] = tool.version
        return dict(self._versions)

    async def _execute(self, slug: str, args: dict) -> dict:
        """One tool call, off the event loop (the Composio SDK is synchronous)."""

        def _call() -> dict:
            resp = self._client.tools.execute(
                slug,
                args,
                user_id=self._user_id,
                version=self._versions.get(slug),
            )
            return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)

        async def _attempt() -> dict:
            try:
                # to_thread cannot be cancelled, so bound the await. The thread
                # may linger, but the event loop is freed and the app can fail
                # or retry instead of blocking its worker indefinitely.
                out = await asyncio.wait_for(
                    asyncio.to_thread(_call), timeout=settings.request_timeout
                )
            except TimeoutError as e:
                raise RetryableError(
                    f"composio {slug} exceeded {settings.request_timeout:.0f}s"
                ) from e
            except Exception as e:  # SDK-level failure (network, 5xx, auth)
                raise _classify(f"{type(e).__name__}: {e}") from e
            if not out.get("successful"):
                raise _classify(str(out.get("error") or "composio call unsuccessful"))
            return out.get("data") or {}

        return await with_backoff(_attempt)

    # -- search ------------------------------------------------------------

    async def search(self, query: str) -> list[SearchHit]:
        ck = key_for("composio_search_web", query)
        cached = self.search_cache.get(ck)
        if cached is None:
            data = await self._execute(SEARCH_TOOL, {"query": query})
            self.search_cache.set(ck, data, meta={"query": query})
            cached = data

        hits: list[SearchHit] = []
        for c in cached.get("citations") or []:
            url = (c.get("url") or c.get("id") or "").strip()
            if url:
                hits.append(
                    SearchHit(url=url, title=c.get("title") or "", query=query)
                )
        return hits

    async def search_answer(self, query: str) -> str:
        """The narrative summary Exa returns alongside citations.

        Useful context for the extractor, but never used as evidence on its
        own — only fetched pages become evidence_urls.
        """
        ck = key_for("composio_search_web", query)
        cached = self.search_cache.get(ck) or {}
        return (cached.get("answer") or "").strip()

    # -- fetch -------------------------------------------------------------

    async def fetch(self, url: str) -> FetchedDoc:
        ck = key_for("composio_fetch_url", url, str(settings.max_doc_chars))
        cached = self.fetch_cache.get(ck)
        if cached is not None:
            return FetchedDoc(
                url=url, text=cached.get("text", ""), ok=bool(cached.get("text")),
                from_cache=True, error=cached.get("error"),
            )

        await self.throttle.acquire(url)
        try:
            data = await self._execute(
                FETCH_TOOL,
                {"urls": [url], "text": True, "max_characters": settings.max_doc_chars},
            )
        except Exception as e:
            doc = FetchedDoc(url=url, text="", ok=False, error=str(e)[:400])
            # Cache permanent failures so a re-run does not re-hammer a dead URL.
            if isinstance(e, PermanentError):
                self.fetch_cache.set(ck, {"text": "", "error": doc.error})
            return doc

        if rid := data.get("requestId"):
            self.request_ids.append(str(rid))

        results = data.get("results") or []
        text = ""
        for r in results:
            if r.get("text"):
                text = r["text"]
                break

        if not text:
            statuses = data.get("statuses") or []
            err = f"empty extraction; statuses={statuses}"[:400]
            self.fetch_cache.set(ck, {"text": "", "error": err})
            return FetchedDoc(url=url, text="", ok=False, error=err)

        self.fetch_cache.set(ck, {"text": text}, meta={"url": url})
        return FetchedDoc(url=url, text=text, ok=True)


# ---------------------------------------------------------------------------
# Keyless fallback
# ---------------------------------------------------------------------------


class DirectProvider:
    """DuckDuckGo HTML search + plain httpx fetch. No API key required.

    Deliberately kept simple: it exists so the pipeline degrades instead of
    dying when COMPOSIO_API_KEY is absent.
    """

    name = "direct"
    UA = "Mozilla/5.0 (compatible; toolkit-recon/0.1; +research)"

    def __init__(self) -> None:
        self.search_cache = DiskCache("direct_search")
        self.fetch_cache = DiskCache("direct_fetch")
        self.throttle = DomainThrottle()
        self.request_ids: list[str] = []
        self._client: httpx.AsyncClient | None = None

    async def prepare(self) -> dict[str, str]:
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            follow_redirects=True,
            headers={"User-Agent": self.UA},
        )
        return {}

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _get(self, url: str, **kw) -> httpx.Response:
        assert self._client is not None, "call prepare() first"

        async def _attempt() -> httpx.Response:
            try:
                r = await self._client.get(url, **kw)
            except httpx.HTTPError as e:
                raise RetryableError(str(e)) from e
            if r.status_code == 404:
                raise PermanentError("404")
            if r.status_code == 429 or r.status_code >= 500:
                raise RetryableError(str(r.status_code))
            if r.status_code >= 400:
                raise PermanentError(str(r.status_code))
            return r

        return await with_backoff(_attempt)

    async def search(self, query: str) -> list[SearchHit]:
        from urllib.parse import parse_qs, unquote, urlparse

        from bs4 import BeautifulSoup

        ck = key_for("ddg", query)
        html = self.search_cache.get(ck)
        if html is None:
            await self.throttle.acquire("https://html.duckduckgo.com")
            r = await self._get(
                "https://html.duckduckgo.com/html/", params={"q": query}
            )
            html = r.text
            self.search_cache.set(ck, html, meta={"query": query})

        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        for a in soup.select("a.result__a")[:12]:
            href = a.get("href", "")
            # DDG wraps results in /l/?uddg=<encoded>
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                href = unquote(qs.get("uddg", [""])[0])
            if href.startswith("http"):
                hits.append(
                    SearchHit(url=href, title=a.get_text(" ", strip=True), query=query)
                )
        return hits

    async def search_answer(self, query: str) -> str:
        return ""

    async def fetch(self, url: str) -> FetchedDoc:
        from bs4 import BeautifulSoup

        ck = key_for("direct_fetch", url, str(settings.max_doc_chars))
        cached = self.fetch_cache.get(ck)
        if cached is not None:
            return FetchedDoc(
                url=url, text=cached.get("text", ""), ok=bool(cached.get("text")),
                from_cache=True, error=cached.get("error"),
            )

        await self.throttle.acquire(url)
        try:
            r = await self._get(url)
        except Exception as e:
            doc = FetchedDoc(url=url, text="", ok=False, error=str(e)[:400])
            if isinstance(e, PermanentError):
                self.fetch_cache.set(ck, {"text": "", "error": doc.error})
            return doc

        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "svg", "noscript"]):
            tag.decompose()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
        text = text[: settings.max_doc_chars]
        self.fetch_cache.set(ck, {"text": text}, meta={"url": url})
        return FetchedDoc(url=url, text=text, ok=bool(text))


def build_provider() -> Provider:
    """Composio when a key is present, keyless fallback otherwise."""
    if settings.composio_api_key:
        return ComposioProvider()
    return DirectProvider()
