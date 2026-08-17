"""Orchestration: search -> rank -> fetch -> extract -> score -> persist.

Per-app isolation is the central robustness property. One app's failure —
network, provider error, schema violation, anything — produces a `low`
confidence row explaining what broke, and the run continues. Nothing that
happens inside `profile_app` can take down the other 99.
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback

from .apps import AppSpec
from .confidence import assign_confidence
from .config import settings
from .extract import Extractor
from .ranking import is_official, rank
from .schema import AppResearch, AppTrace, FetchedDoc
from .storage import Checkpoint, TraceLog, save_evidence
from .throttle import DailyQuotaExhausted, domain_of


# Each pass asks differently on purpose. Re-running the same two queries would
# hit the same cache and reproduce the same row, which measures nothing. Fresh
# angles surface different pages, and the disagreement between passes is the
# accuracy delta `pass_number` exists to capture.
QUERY_SETS: dict[int, tuple[str, str]] = {
    1: ("{name} API documentation authentication",
        "{name} API pricing developer access"),
    2: ("{name} developer portal REST API reference authentication",
        "{name} API access requirements plan admin approval"),
    3: ("{name} official MCP server model context protocol",
        "{name} API getting started OAuth scopes rate limits"),
}


# Word-boundary "mcp" so "mcpherson" and similar do not register as a hit.
MCP_MENTION = re.compile(r"model context protocol|\bmcp\b", re.I)


def queries_for(app: AppSpec, pass_number: int = 1) -> list[str]:
    return [q.format(name=app.name) for q in QUERY_SETS.get(pass_number, QUERY_SETS[1])]


def failure_row(app: AppSpec, reason: str, pass_number: int) -> AppResearch:
    """The row emitted when an app blows up. Never raises, always schema-valid."""
    return AppResearch(
        name=app.name,
        category=app.category,
        one_liner="",
        auth_methods=["Unknown"],
        access_tier="no_public_api",
        api_style=["None"],
        api_breadth="narrow",
        has_mcp=False,
        mcp_evidence_url=None,
        buildable_today="no",
        primary_blocker="research failed; not assessed",
        # Schema demands >=1 evidence URL. A homepage guess would be a lie, so
        # we record the search we issued instead — honest and auditable.
        evidence_urls=[f"unresolved://{app.slug}"],
        confidence="low",
        agent_notes=f"RESEARCH FAILED: {reason}",
        pass_number=pass_number,
    )


class Pipeline:
    def __init__(self, provider, extractor: Extractor, pass_number: int = 1) -> None:
        self.provider = provider
        self.extractor = extractor
        self.pass_number = pass_number
        self.sem = asyncio.Semaphore(settings.concurrency)
        self.trace_log = TraceLog()
        self.checkpoint = Checkpoint(pass_number)
        # Set once the provider's daily budget is gone. Every remaining app
        # would fail identically, so the run stops rather than manufacturing
        # 48 low-confidence rows that say nothing about those products.
        self._aborted = False
        self.abort_reason: str | None = None

    # ---------------- one app ----------------

    async def profile_app(self, app: AppSpec) -> tuple[AppResearch, AppTrace]:
        started = time.monotonic()
        trace = AppTrace(slug=app.slug, name=app.name, pass_number=self.pass_number)
        trace.llm_model = settings.llm_model

        try:
            # A deadline, not just retries. Cancelling the coroutine also frees
            # this app's concurrency slot, so one wedged app cannot starve the
            # other 99 of workers.
            row = await asyncio.wait_for(
                self._profile_inner(app, trace), timeout=settings.app_deadline
            )
            trace.status = "ok"
        except TimeoutError as e:
            reason = (f"exceeded the {settings.app_deadline:.0f}s per-app deadline "
                      f"(stalled after {len(trace.urls_fetched)} document(s))")
            trace.status = "failed"
            trace.error = reason
            trace.final_confidence = "low"
            trace.confidence_reason = "per-app deadline exceeded"
            row = failure_row(app, reason, self.pass_number)
            del e
        except DailyQuotaExhausted as e:
            # Not this app's fault and not recoverable by retrying. Record it
            # honestly and signal the run to stop starting new work.
            self._aborted = True
            self.abort_reason = str(e)
            trace.status = "failed"
            trace.error = str(e)[:600]
            trace.final_confidence = "low"
            trace.confidence_reason = "provider daily token budget exhausted"
            row = failure_row(app, f"provider daily token budget exhausted: {e}"[:300],
                              self.pass_number)
            trace.wall_time_s = round(time.monotonic() - started, 2)
            return row, trace
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            trace.status = "failed"
            trace.error = reason[:600]
            trace.final_confidence = "low"
            trace.confidence_reason = "unhandled error during research"
            row = failure_row(app, reason[:300], self.pass_number)
            # Full stack goes to the raw dir, not the trace line — keeps
            # trace.jsonl readable while preserving the detail for debugging.
            try:
                d = settings.raw_dir / app.slug
                d.mkdir(parents=True, exist_ok=True)
                # .log, not .txt: evidence files are *.txt and an auditor
                # globbing the folder should not pick up stack traces.
                (d / "_error.log").write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass

        trace.wall_time_s = round(time.monotonic() - started, 2)
        return row, trace

    async def _profile_inner(self, app: AppSpec, trace: AppTrace) -> AppResearch:
        # --- 1. search -------------------------------------------------
        qs = queries_for(app, self.pass_number)
        trace.queries = qs

        hits = []
        answers = []
        for q in qs:
            hits.extend(await self.provider.search(q))
            if hint := await self.provider.search_answer(q):
                answers.append(hint)

        # --- 2. rank ---------------------------------------------------
        ranked = rank(hits, app.official_domains, settings.max_docs_per_app)
        trace.urls_ranked = ranked

        if not ranked:
            raise RuntimeError("search returned no usable URLs")

        # --- 3. fetch --------------------------------------------------
        docs: list[FetchedDoc] = []
        for hit in ranked:
            doc = await self.provider.fetch(hit.url)
            doc.is_official = is_official(hit.url, app.official_domains)
            docs.append(doc)

        # Thin extractions are archived but excluded from evidence: a nav
        # shell or cookie wall must not satisfy "official docs reached".
        good = [d for d in docs if d.ok and len(d.text) >= settings.min_doc_chars]
        thin = [d for d in docs if d.ok and len(d.text) < settings.min_doc_chars]
        trace.urls_fetched = [d.url for d in good]
        trace.urls_failed = [d.url for d in docs if not d.ok]
        trace.urls_thin = [d.url for d in thin]
        trace.cache_hits = sum(1 for d in docs if d.from_cache)
        trace.cache_misses = sum(1 for d in docs if not d.from_cache)
        official_reached = sorted(
            {domain_of(d.url) for d in good if d.is_official}
        )
        trace.official_domains_reached = official_reached

        save_evidence(
            app.slug,
            docs,
            {
                "name": app.name,
                "category": app.category,
                "queries": qs,
                "pass_number": self.pass_number,
                "provider": getattr(self.provider, "name", "unknown"),
            },
        )

        if not good:
            detail = "; ".join(
                f"{d.url} ({d.error or f'{len(d.text)} chars, below threshold'})"
                for d in docs
            )
            raise RuntimeError(
                f"no usable evidence from {len(docs)} candidate URLs: {detail[:300]}"
            )

        # --- 4. extract (structured output) ----------------------------
        payload = [(d.url, d.text) for d in good]
        extraction, tokens = await self.extractor.extract(
            app.name, app.category, payload, hint="\n\n".join(answers)[:2000]
        )
        trace.prompt_tokens = tokens["prompt_tokens"]
        trace.completion_tokens = tokens["completion_tokens"]
        trace.total_tokens = tokens["total_tokens"]

        # --- 5. confidence, derived in code ----------------------------
        conf, reason = assign_confidence(
            extraction,
            official_docs_reached=bool(official_reached),
            docs_fetched=len(good),
        )
        trace.final_confidence = conf
        trace.confidence_reason = reason
        trace.composio_request_ids = list(getattr(self.provider, "request_ids", []))[-3:]

        # Evidence URLs are intersected with what we actually retrieved, so the
        # model cannot cite a page the pipeline never fetched.
        fetched = {d.url for d in good}
        cited = [u for u in extraction.evidence_urls if u in fetched]
        evidence = cited or sorted(fetched)

        # An MCP claim has to survive two checks: the cited page must be one we
        # actually fetched, and that page must actually mention MCP. Checking
        # the text catches the case where the model attaches a real URL to an
        # invented finding — a plausible-looking citation is the failure mode
        # a URL-only check cannot see.
        by_url = {d.url: d.text for d in good}
        mcp_url = extraction.mcp_evidence_url
        mcp_note = ""
        if mcp_url and mcp_url not in by_url:
            mcp_url, mcp_note = None, " [pipeline: MCP citation was not in the fetched set]"
        elif mcp_url and not MCP_MENTION.search(by_url[mcp_url]):
            mcp_url, mcp_note = None, " [pipeline: cited MCP page does not mention MCP]"

        notes = extraction.agent_notes + mcp_note
        if extraction.evidence_urls and not cited:
            notes += " [pipeline: model cited URLs outside the fetched set; replaced with fetched URLs]"
        if not official_reached:
            notes += " [pipeline: no official vendor domain reached]"

        return AppResearch(
            name=app.name,
            category=app.category,
            one_liner=extraction.one_liner,
            auth_methods=extraction.auth_methods,
            access_tier=extraction.access_tier,
            api_style=extraction.api_style,
            api_breadth=extraction.api_breadth,
            has_mcp=extraction.has_mcp and mcp_url is not None,
            mcp_evidence_url=mcp_url,
            buildable_today=extraction.buildable_today,
            primary_blocker=extraction.primary_blocker,
            evidence_urls=evidence,
            confidence=conf,
            agent_notes=notes.strip(),
            pass_number=self.pass_number,
        )

    # ---------------- the run ----------------

    async def run(self, apps: list[AppSpec], on_done=None) -> dict[str, AppResearch]:
        """Profile every app, keyed by slug. Never raises for a single app."""
        results: dict[str, AppResearch] = {}

        async def worker(app: AppSpec) -> None:
            if self._aborted:
                return  # daily budget gone; do not start new work
            async with self.sem:
                if self._aborted:
                    return
                row, trace = await self.profile_app(app)
            results[app.slug] = row
            await self.trace_log.write(trace)
            await self.checkpoint.record(app.slug, row)  # flush after EVERY app
            if on_done:
                on_done(app, row, trace)

        await asyncio.gather(*(worker(a) for a in apps))
        return results
