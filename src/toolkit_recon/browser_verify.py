"""LAYER 3 — browser verification of disputed fields only.

Expensive, so scoped hard: only rows Layer 2 could not settle.

The trust discipline from the earlier layers carries through unchanged. A model
never hands us a verdict we accept on faith. It must return a **verbatim quote**
from the live page, and this module checks that the quote literally occurs in
the DOM text Playwright just read. A verdict whose quote cannot be found is
discarded and the field is recorded ``unresolvable`` — an invented citation
fails closed rather than resolving a dispute.

What the model does is bounded reading comprehension over text we captured
ourselves; what the code does is decide the resolution label. Resolutions:

    pass1_correct | pass2_correct | both_wrong | unresolvable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from .apps import APPS
from .config import settings
from .extract import strict_schema
from .ratelimit import TokenRateLimiter, estimate_tokens
from .ranking import is_official
from .throttle import PermanentError, RetryableError, with_backoff

SCREENSHOT_DIR = settings.root / "evidence" / "screenshots"

# Sections worth scrolling to before capturing.
SECTION_HINTS = [
    "authentication", "authorization", "oauth", "api key", "access token",
    "pricing", "plans", "developer", "getting started", "rate limit",
]


class BrowserVerdict(BaseModel):
    """What the judge model may return. `quote` is the load-bearing field."""

    quote: str = Field(
        description=(
            "A VERBATIM span copied exactly from the supplied page text that "
            "settles the question. Must appear character-for-character in the "
            "page. Empty string if the page does not settle it."
        )
    )
    supports: Literal["pass1", "pass2", "neither", "unclear"] = Field(
        description=(
            "Which candidate value the quote supports. 'neither' only if the "
            "quote clearly contradicts both. 'unclear' if the page is silent."
        )
    )
    reasoning: str = Field(description="One sentence tying the quote to the choice.")


JUDGE_SYSTEM = """\
You are verifying a disputed factual claim about a SaaS product's API, using
ONLY the page text supplied to you. Two earlier research passes disagreed and
you are the tiebreaker.

The single most important rule: `quote` must be copied VERBATIM from the page
text — character for character, no paraphrasing, no ellipsis, no correction of
typos or spacing. Your answer is discarded automatically if the quote cannot be
found in the page. A short exact quote beats a long approximate one.

If the page does not actually settle the question, return an empty quote and
`supports: "unclear"`. That is a correct and useful answer. Guessing is not.
"""


def _norm(s: str) -> str:
    """Collapse whitespace so quote matching is not defeated by re-wrapping."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def quote_is_grounded(quote: str, page_text: str, min_len: int = 25) -> bool:
    """The verification gate. Code-side, no model involvement."""
    q, p = _norm(quote), _norm(page_text)
    if len(q) < min_len:
        return False
    return q in p


# ---------------------------------------------------------------------------
# Browser capture
# ---------------------------------------------------------------------------


async def capture(url: str, slug: str) -> tuple[str, str | None, str | None]:
    """Load `url`, return (page_text, screenshot_path, error)."""
    from playwright.async_api import async_playwright

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    shot = SCREENSHOT_DIR / f"{slug}.png"

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass  # a docs site that never idles is still readable

                # Nudge lazy content into the DOM before reading it.
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await page.wait_for_timeout(1200)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(400)

                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                await page.screenshot(path=str(shot), full_page=False)
                return text or "", str(shot), None
            finally:
                await browser.close()
    except Exception as e:
        return "", None, f"{type(e).__name__}: {e}"[:300]


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


class Judge:
    def __init__(self) -> None:
        if not settings.llm_api_key:
            raise RuntimeError("No LLM key. Set GROQ_API_KEY or LLM_API_KEY.")
        self._schema = strict_schema(BrowserVerdict)
        self.limiter = TokenRateLimiter(
            tokens_per_minute=settings.llm_tokens_per_minute
        )
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=settings.request_timeout,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def rule(
        self, app: str, field: str, v1, v2, page_text: str, url: str
    ) -> BrowserVerdict | None:
        excerpt = _relevant_excerpt(page_text, settings.prompt_doc_budget)
        user = (
            f"PRODUCT: {app}\nDISPUTED FIELD: {field}\n"
            f"PASS 1 SAID: {json.dumps(v1)}\n"
            f"PASS 2 SAID: {json.dumps(v2)}\n\n"
            f"LIVE PAGE ({url}):\n---\n{excerpt}\n---\n\n"
            "Which value does this page support? Quote verbatim."
        )
        body = {
            "model": settings.llm_model,
            "temperature": 0,
            "max_completion_tokens": settings.llm_max_completion_tokens,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "browser_verdict", "strict": True,
                    "schema": self._schema,
                },
            },
        }
        est = (estimate_tokens(JUDGE_SYSTEM) + estimate_tokens(user)
               + settings.llm_expected_completion_tokens)

        async def _attempt() -> httpx.Response:
            await self.limiter.acquire(est)
            try:
                r = await self._client.post("/chat/completions", json=body)
            except httpx.HTTPError as e:
                raise RetryableError(str(e)) from e
            if r.status_code == 429:
                ra = r.headers.get("retry-after")
                try:
                    delay = float(ra) if ra else 20.0
                except ValueError:
                    delay = 20.0
                self.limiter.penalise(min(delay + 1.0, 90.0))
                raise RetryableError("429")
            if r.status_code >= 500:
                raise RetryableError(str(r.status_code))
            if r.status_code >= 400:
                low = r.text.lower()
                if any(s in low for s in ("json_validate_failed",
                                          "failed to generate json",
                                          "failed to validate json")):
                    raise RetryableError("json decode")
                raise PermanentError(f"{r.status_code}: {r.text[:200]}")
            if (r.json().get("choices") or [{}])[0].get("finish_reason") == "length":
                raise RetryableError("truncated")
            return r

        try:
            resp = await with_backoff(_attempt, attempts=settings.llm_retries)
        except Exception:
            return None
        payload = resp.json()
        self.limiter.settle(est, int((payload.get("usage") or {}).get("total_tokens") or 0))
        try:
            return BrowserVerdict.model_validate(
                json.loads(payload["choices"][0]["message"]["content"])
            )
        except Exception:
            return None


def _relevant_excerpt(text: str, budget: int) -> str:
    """Keep the parts of a live page most likely to settle auth/pricing."""
    from .condense import condense

    return condense(text, budget)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _target_url(row1: dict, row2: dict, official: tuple[str, ...]) -> str | None:
    """Prefer an official docs URL; fall back to any http evidence URL."""
    seen: list[str] = []
    for r in (row1, row2):
        for u in r.get("evidence_urls") or []:
            if u.startswith("http") and u not in seen:
                seen.append(u)
    for u in seen:
        if is_official(u, official):
            return u
    return seen[0] if seen else None


async def run(limit: int | None = None) -> dict:
    dd = settings.data_dir
    disputes = json.loads((dd / "disagreements.json").read_text(encoding="utf-8"))
    rows1 = {r["name"]: r for r in json.loads(
        (dd / "pass1.validated.json").read_text(encoding="utf-8"))}
    rows2 = {r["name"]: r for r in json.loads(
        (dd / "pass2.json").read_text(encoding="utf-8"))}
    by_name = {a.name: a for a in APPS}

    grouped: dict[str, list[dict]] = {}
    for d in disputes:
        grouped.setdefault(d["app"], []).append(d)

    names = sorted(grouped)
    if limit:
        names = names[:limit]

    judge = Judge()
    results: list[dict] = []
    out_rows: list[dict] = []

    try:
        for i, name in enumerate(names, 1):
            app = by_name.get(name)
            r1, r2 = rows1.get(name, {}), rows2.get(name, {})
            url = _target_url(r1, r2, app.official_domains if app else ())
            slug = app.slug if app else re.sub(r"\W+", "_", name.lower())

            if not url:
                results.append({"app": name, "slug": slug, "url": None,
                                "error": "no evidence URL to verify",
                                "fields": [{"field": d["field"],
                                            "resolution": "unresolvable"}
                                           for d in grouped[name]]})
                print(f"[{i}/{len(names)}] {name:<28} no URL -> unresolvable")
                continue

            text, shot, err = await capture(url, slug)
            field_results: list[dict] = []

            # Same floor as Layer 1, for the same reason. Some docs sites serve
            # headless browsers a stub (Notion returns an llms.txt pointer of
            # ~2k chars, not the reference). Judging a dispute against a stub
            # would manufacture a resolution out of a page we never really read.
            too_thin = not err and len(text) < settings.min_browser_chars

            for d in grouped[name]:
                if err or too_thin:
                    field_results.append({
                        "field": d["field"], "resolution": "unresolvable",
                        "reason": err or (
                            f"captured page too thin ({len(text)} chars < "
                            f"{settings.min_browser_chars}); likely a stub "
                            f"served to headless browsers"
                        ),
                        "pass1": d["pass1"], "pass2": d["pass2"],
                    })
                    continue

                v = await judge.rule(name, d["field"], d["pass1"], d["pass2"],
                                     text, url)
                if v is None:
                    field_results.append({
                        "field": d["field"], "resolution": "unresolvable",
                        "reason": "judge call failed",
                        "pass1": d["pass1"], "pass2": d["pass2"],
                    })
                    continue

                grounded = quote_is_grounded(v.quote, text)
                if not grounded:
                    # The gate: an ungrounded quote resolves nothing.
                    resolution = "unresolvable"
                    reason = ("quote not found verbatim in live page"
                              if v.quote else "no quote offered")
                elif v.supports == "pass1":
                    resolution, reason = "pass1_correct", v.reasoning
                elif v.supports == "pass2":
                    resolution, reason = "pass2_correct", v.reasoning
                elif v.supports == "neither":
                    resolution, reason = "both_wrong", v.reasoning
                else:
                    resolution, reason = "unresolvable", "page does not settle it"

                field_results.append({
                    "field": d["field"], "resolution": resolution,
                    "reason": reason, "quote": v.quote if grounded else None,
                    "quote_grounded": grounded,
                    "pass1": d["pass1"], "pass2": d["pass2"],
                })

            results.append({"app": name, "slug": slug, "url": url,
                            "screenshot": shot, "error": err,
                            "fields": field_results})

            # Build the pass-3 row: apply only fields the browser actually settled.
            row = dict(r2) or dict(r1)
            row["pass_number"] = 3
            applied = []
            for fr in field_results:
                if fr["resolution"] == "pass1_correct":
                    row[fr["field"]] = r1.get(fr["field"])
                    applied.append(f"{fr['field']}<-pass1")
                elif fr["resolution"] == "pass2_correct":
                    row[fr["field"]] = r2.get(fr["field"])
                    applied.append(f"{fr['field']}<-pass2")
            unresolved = [f["field"] for f in field_results
                          if f["resolution"] in {"unresolvable", "both_wrong"}]
            note = f" [layer3: browser-verified {', '.join(applied) or 'nothing'}"
            if unresolved:
                note += f"; unresolved: {', '.join(unresolved)}"
            row["agent_notes"] = (row.get("agent_notes", "") + note + "]").strip()
            if unresolved:
                row["confidence"] = "low"
            out_rows.append(row)

            tally = ", ".join(f"{f['field']}={f['resolution']}" for f in field_results)
            print(f"[{i}/{len(names)}] {name:<28} {tally}")
    finally:
        await judge.aclose()

    (dd / "pass3.json").write_text(
        json.dumps(out_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    (dd / "browser_verification.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    counts: dict[str, int] = {}
    for r in results:
        for f in r["fields"]:
            counts[f["resolution"]] = counts.get(f["resolution"], 0) + 1
    summary = {"apps_verified": len(results),
               "fields_examined": sum(len(r["fields"]) for r in results),
               "resolutions": counts,
               "screenshots_dir": str(SCREENSHOT_DIR)}
    (dd / "layer3_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-browser")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)

    s = asyncio.run(run(args.limit))
    print("\n" + "=" * 66)
    print("LAYER 3 — BROWSER VERIFICATION")
    print("=" * 66)
    print(f"  apps verified      : {s['apps_verified']}")
    print(f"  fields examined    : {s['fields_examined']}")
    for k, n in sorted(s["resolutions"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:<18}{n:>4}")
    print(f"\n  screenshots -> {s['screenshots_dir']}")
    print("  wrote data/pass3.json, data/browser_verification.json,")
    print("        data/layer3_summary.json")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
