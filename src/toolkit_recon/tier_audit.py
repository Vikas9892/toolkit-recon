"""Is the access_tier distribution a finding, or is it extractor optimism?

Interim pass 1 came back `self_serve_free` on 29 of 32 rows with only four
rows naming a blocker. That is either a real property of the corpus or an
artefact of how the field gets filled, and the difference matters more than
the number does. Three checks here, none of which need an LLM:

1. `tier_by_confidence` — split the tier distribution by confidence cohort. If
   `self_serve_free` dominates only where confidence is weak, the field is
   inference under uncertainty rather than an observation.

2. `pricing_evidence` — for every self-serve row, ask whether any fetched page
   actually contained pricing or access language at all. `access_tier` has no
   member for "not determinable from public docs", so absence of gating
   language has nowhere to go except `self_serve`. Docs pages describe the
   API; the gate usually lives on a pricing page, or nowhere.

3. `hand_check_queue` — stage the rows a human should verify against vendor
   pricing and partner pages, since only a human can settle it.

The schema gap in (2) is deliberately *measured*, not fixed. Adding an enum
member mid-run would invalidate every row already collected.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

from .apps import APPS
from .config import settings

TIERS = ["self_serve_free", "self_serve_trial", "paid_plan_required",
         "admin_approval", "partner_gated", "no_public_api"]
SELF_SERVE = {"self_serve_free", "self_serve_trial"}

# Language that indicates the page discussed commercial access at all. Absence
# of every one of these means the tier could not have been read off the page.
PRICING_TERMS = [
    "pricing", "plan", "tier", "free tier", "contact sales", "request access",
    "partner", "enterprise", "subscription", "billing", "upgrade", "quota",
    "trial", "paid", "premium", "license", "seat",
]
_PRICING_RE = re.compile("|".join(re.escape(t) for t in PRICING_TERMS), re.I)

# Products in the corpus whose access is commercially gated in practice, so a
# self-serve verdict on one of them is a candidate false negative worth a human.
EXPECTED_GATED = [
    "Workday", "Oracle NetSuite", "WhatsApp Business Platform",
    "Adobe Marketo Engage", "Tableau", "Looker", "Salesforce",
    "Microsoft Power BI", "Snowflake", "Databricks", "Kustomer", "Iterable",
    "Braze", "BILL", "Rippling", "Outreach", "Salesloft", "Apollo.io",
    "Gorgias", "Deel", "Plaid", "Brex", "Ramp", "Segment",
]

# Requested for hand-checking but absent from the 100-app corpus. Recorded so
# the gap is visible rather than quietly substituted.
REQUESTED_NOT_IN_CORPUS = [
    "DealCloud", "Salesforce Commerce Cloud", "Amazon Selling Partner",
    "PitchBook", "Gladly", "Bright Data", "Clay",
]


# ---------------------------------------------------------------------------


def tier_by_confidence(rows: list[dict]) -> dict:
    high = [r for r in rows if r["confidence"] == "high"]
    weak = [r for r in rows if r["confidence"] != "high"]

    def dist(subset: list[dict]) -> dict[str, int]:
        c = Counter(r["access_tier"] for r in subset)
        return {t: c.get(t, 0) for t in TIERS}

    d_high, d_weak = dist(high), dist(weak)
    share_high = (sum(d_high[t] for t in SELF_SERVE) / len(high)) if high else None
    share_weak = (sum(d_weak[t] for t in SELF_SERVE) / len(weak)) if weak else None

    verdict = "insufficient data in one cohort to compare"
    gap = None
    if share_high is not None and share_weak is not None:
        gap = round(share_weak - share_high, 4)
        if len(high) < 5 or len(weak) < 5:
            verdict = ("cohorts too small to conclude; treat the distribution "
                       "as provisional")
        elif gap > 0.15:
            verdict = ("self-serve dominates the weak cohort specifically -- "
                       "the tier reads as inference under uncertainty, not an "
                       "observed property of the corpus")
        elif gap < -0.15:
            verdict = ("self-serve is concentrated in the HIGH cohort, which "
                       "is the opposite of an optimism artefact")
        else:
            verdict = ("self-serve share is similar across cohorts, so the "
                       "distribution is not explained by confidence alone")

    return {
        "high_cohort_n": len(high),
        "weak_cohort_n": len(weak),
        "tier_distribution_high": d_high,
        "tier_distribution_weak": d_weak,
        "self_serve_share_high": (round(share_high, 4)
                                  if share_high is not None else None),
        "self_serve_share_weak": (round(share_weak, 4)
                                  if share_weak is not None else None),
        "share_gap_weak_minus_high": gap,
        "verdict": verdict,
    }


def _evidence_text(slug: str) -> str:
    d = settings.raw_dir / slug
    if not d.is_dir():
        return ""
    parts = []
    for f in sorted(d.glob("*.txt")):
        try:
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def pricing_evidence(rows: list[dict]) -> dict:
    """How many self-serve verdicts rest on pages that never discussed access?"""
    by_name = {a.name: a.slug for a in APPS}
    self_serve = [r for r in rows if r["access_tier"] in SELF_SERVE]

    without: list[dict] = []
    with_terms = 0
    for r in self_serve:
        slug = by_name.get(r["name"])
        text = _evidence_text(slug) if slug else ""
        hits = sorted({m.group(0).lower() for m in _PRICING_RE.finditer(text)})
        if hits:
            with_terms += 1
        else:
            without.append({
                "app": r["name"], "slug": slug, "tier": r["access_tier"],
                "confidence": r["confidence"],
                "evidence_chars": len(text),
            })

    n = len(self_serve)
    share = round(len(without) / n, 4) if n else None
    if share is None:
        verdict = "no self-serve rows to assess"
    elif share >= 0.5:
        verdict = ("most self-serve verdicts rest on pages that never mention "
                   "pricing or access at all -- the field was inferred from "
                   "silence, and the distribution should be read as a limit of "
                   "the method rather than a property of the corpus")
    elif share >= 0.2:
        verdict = ("a substantial minority of self-serve verdicts have no "
                   "pricing evidence behind them; those rows are inferences")
    else:
        verdict = ("most self-serve verdicts are backed by pages that do "
                   "discuss access terms")

    return {
        "self_serve_rows": n,
        "with_pricing_language": with_terms,
        "without_any_pricing_language": len(without),
        "share_without": share,
        "rows_without_pricing_evidence": without,
        "schema_note": (
            "access_tier has no member for 'not determinable from public "
            "docs'. Absence of gating language therefore has nowhere to go "
            "except a self_serve value. Not changed mid-run: adding an enum "
            "member would invalidate every row already collected."
        ),
        "verdict": verdict,
    }


def hand_check_queue(rows: list[dict]) -> tuple[Path, dict]:
    """CSV of rows a human should verify against vendor pricing/partner pages."""
    by_name = {a.name: a.slug for a in APPS}
    present = {r["name"] for r in rows}

    picked: list[tuple[str, dict]] = []
    for r in rows:
        if r["name"] in EXPECTED_GATED and r["access_tier"] in SELF_SERVE:
            picked.append(("expected-gated but returned self-serve", r))
    for r in rows:
        if r["access_tier"] not in SELF_SERVE:
            picked.append(("returned gated -- check it is not a false positive", r))

    seen: set[str] = set()
    ordered = []
    for reason, r in picked:
        if r["name"] not in seen:
            seen.add(r["name"])
            ordered.append((reason, r))

    path = settings.data_dir / "hand_check_queue.csv"
    cols = ["slug", "name", "category", "why_selected", "agent_access_tier",
            "agent_confidence", "agent_primary_blocker", "evidence_urls",
            "truth_access_tier", "why_it_failed"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for reason, r in ordered:
            w.writerow({
                "slug": by_name.get(r["name"], ""),
                "name": r["name"], "category": r["category"],
                "why_selected": reason,
                "agent_access_tier": r["access_tier"],
                "agent_confidence": r["confidence"],
                "agent_primary_blocker": r.get("primary_blocker") or "",
                "evidence_urls": " | ".join(r.get("evidence_urls") or []),
                "truth_access_tier": "", "why_it_failed": "",
            })

    meta = {
        "queue_size": len(ordered),
        "requested_but_not_in_corpus": REQUESTED_NOT_IN_CORPUS,
        "requested_not_in_corpus_note": (
            "None of the seven products requested for hand-checking exist in "
            "the 100-app corpus, so none could be queued. They were NOT "
            "silently replaced. The queue below is a substitute drawn from the "
            "corpus: products whose access is commercially gated in practice "
            "but which the pipeline called self-serve (candidate false "
            "negatives), plus every row it did call gated (candidate false "
            "positives)."
        ),
        "expected_gated_present_in_corpus": sorted(
            n for n in EXPECTED_GATED if n in present),
        "rows": [{"name": r["name"], "why": reason} for reason, r in ordered],
    }
    (settings.data_dir / "hand_check_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return path, meta
