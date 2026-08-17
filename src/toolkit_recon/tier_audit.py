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

4. `score_hand_check` — read that queue back once a human has filled it, and
   state whether the self-serve figure survived.

The schema gap in (2) is deliberately *measured*, not fixed. Adding an enum
member mid-run would invalidate every row already collected.

(4) is the only check here that can separate the two live explanations for a
~90% self-serve corpus: that the corpus really is mostly self-serve, or that
the extractor reads *documented API* as *obtainable credentials*. (1) cannot,
and not because it was measured badly — a bias that is uniform across
confidence cohorts produces a null result in a tier x confidence cross-tab by
construction. The cross-tab can only detect a bias that *concentrates* in the
weak cohort. Ground truth is the only thing that distinguishes them, which is
why the queue exists and why nothing downstream should quote the headline
until it is filled.
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


class HandCheckAlreadyFilled(RuntimeError):
    """Raised rather than overwrite truth a human has already established."""


def _filled_truth_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            return sum(1 for r in csv.DictReader(fh)
                       if (r.get("truth_access_tier") or "").strip())
    except (OSError, csv.Error):
        return 0


def hand_check_queue(rows: list[dict], force: bool = False) -> tuple[Path, dict]:
    """CSV of rows a human should verify against vendor pricing/partner pages.

    Refuses to overwrite a queue whose truth column has already been filled.
    Regenerating is cheap and the fill is not: it is the one artefact here that
    cost someone an hour of reading vendor pricing pages, and it cannot be
    reconstructed from anything else in the repo.
    """
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
    filled = _filled_truth_count(path)
    if filled and not force:
        raise HandCheckAlreadyFilled(
            f"{path} already has {filled} verified row(s) in truth_access_tier. "
            "Regenerating would discard them and they cannot be rebuilt from "
            "anything else in the repo.\n"
            "  - to score what is there:  --hand-check-score\n"
            "  - to rebuild anyway:       --hand-check --force "
            "(copy the file somewhere first)"
        )
    cols = ["slug", "name", "category", "why_selected", "agent_access_tier",
            "agent_confidence", "agent_primary_blocker", "evidence_urls",
            "truth_access_tier", "truth_evidence_url", "why_it_failed"]
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
                "truth_access_tier": "", "truth_evidence_url": "",
                "why_it_failed": "",
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


# ---------------------------------------------------------------------------
# Scoring the filled queue
# ---------------------------------------------------------------------------

# A row whose research failed never produced a tier verdict to be right or
# wrong about. `no_public_api` on such a row is the absence of a reading, not
# a claim of one, and folding it into the false-positive rate would credit the
# pipeline with a gated call it never actually made.
_RESEARCH_FAILED_MARKS = ("research failed", "not assessed")

# Thresholds for the verdict line. Deliberately blunt: the queue is small, and
# a rate this coarse either survives a large gap or it does not.
_FN_SYSTEMATIC = 0.50   # at or above: the figure is an artefact of extraction
_FN_INFLATED = 0.25     # at or above: real signal, but the number is overstated
_FN_SURVIVES = 0.15     # at or below: the corpus really is mostly self-serve
_MIN_TO_CONCLUDE = 5    # self-serve rows that must be checked to say anything


def _is_research_failure(row: dict) -> bool:
    blocker = (row.get("agent_primary_blocker") or "").lower()
    evidence = (row.get("evidence_urls") or "").strip().lower()
    return (any(m in blocker for m in _RESEARCH_FAILED_MARKS)
            or evidence.startswith("unresolved://"))


def score_hand_check(csv_path: Path, corpus_rows: list[dict] | None = None) -> dict:
    """Read the completed hand-check CSV and say whether ~90% self-serve holds.

    `truth_access_tier` is the human's reading of the vendor's own pricing or
    partner page. Everything here is counted against that column and nothing
    else; a blank truth is an unchecked row, never an agreement.
    """
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    per_app: list[dict] = []
    unchecked: list[str] = []
    bad_vocab: list[dict] = []
    research_failed: list[dict] = []

    fn_denom = fn_num = 0          # agent said self-serve
    fp_denom = fp_num = 0          # agent said gated
    same_class_mismatch = 0

    for r in rows:
        name = (r.get("name") or "").strip()
        agent = (r.get("agent_access_tier") or "").strip()
        truth = (r.get("truth_access_tier") or "").strip().lower()

        if not truth:
            unchecked.append(name)
            continue
        if truth not in TIERS:
            bad_vocab.append({"app": name, "value": truth})
            continue

        agent_self = agent in SELF_SERVE
        truth_self = truth in SELF_SERVE

        if agent == truth:
            outcome = "correct"
        elif agent_self and not truth_self:
            outcome = "false_negative"      # missed a gate that is really there
        elif not agent_self and truth_self:
            outcome = "false_positive"      # invented a gate
        else:
            outcome = "same_class_mismatch"  # wrong member, right side of the line

        entry = {
            "app": name,
            "slug": (r.get("slug") or "").strip(),
            "why_selected": (r.get("why_selected") or "").strip(),
            "agent_access_tier": agent,
            "agent_confidence": (r.get("agent_confidence") or "").strip(),
            "truth_access_tier": truth,
            "outcome": outcome,
            "vendor_evidence_url": (r.get("truth_evidence_url") or "").strip(),
            "why_it_failed": (r.get("why_it_failed") or "").strip(),
        }

        if _is_research_failure(r):
            # Recorded in full, but kept out of both rates.
            entry["excluded_from_rates"] = (
                "research failed for this app, so the pipeline never made a "
                "tier claim that could be scored")
            research_failed.append(entry)
            per_app.append(entry)
            continue

        if not entry["vendor_evidence_url"]:
            entry["evidence_note"] = (
                "no vendor URL recorded; the verdict is asserted, not cited")

        if agent_self:
            fn_denom += 1
            fn_num += outcome == "false_negative"
        else:
            fp_denom += 1
            fp_num += outcome == "false_positive"
        same_class_mismatch += outcome == "same_class_mismatch"
        per_app.append(entry)

    fn_rate = round(fn_num / fn_denom, 4) if fn_denom else None
    fp_rate = round(fp_num / fp_denom, 4) if fp_denom else None

    return {
        "source_csv": str(csv_path),
        "filled_by": _provenance(),
        "rows_in_queue": len(rows),
        "rows_scored": len(per_app) - len(research_failed),
        "rows_not_yet_checked": unchecked,
        "invalid_truth_values": bad_vocab,
        "truth_vocabulary": TIERS,
        "excluded_research_failures": [e["app"] for e in research_failed],
        "false_negative_rate": {
            "definition": (
                "of the queued rows the pipeline called self-serve, the share "
                "the vendor's own page shows to be gated"),
            "checked": fn_denom, "wrong": fn_num, "rate": fn_rate,
        },
        "false_positive_rate": {
            "definition": (
                "of the queued rows the pipeline called gated, the share the "
                "vendor's own page shows to be self-serve"),
            "checked": fp_denom, "wrong": fp_num, "rate": fp_rate,
        },
        "same_class_mismatches": same_class_mismatch,
        "per_app": per_app,
        "corpus_projection": _project(fn_rate, corpus_rows),
        "why_this_test_and_not_the_cross_tab": (
            "Two explanations survive for a ~90% self-serve corpus: the corpus "
            "really is mostly self-serve, or the extractor reads documented "
            "API as obtainable credentials. The tier x confidence cross-tab "
            "cannot separate them. A bias uniform across confidence cohorts "
            "produces a null result there BY CONSTRUCTION -- the cross-tab "
            "detects only a bias that concentrates in the weak cohort, so its "
            "null result is evidence about confidence, not about correctness. "
            "Ground truth from the vendor's own page is the only test that "
            "distinguishes them, and that is what this file scores."
        ),
        "sample_note": (
            "This queue is not a random sample. It was deliberately enriched "
            "with products whose access is commercially gated in practice, so "
            "the false-negative rate here is NOT a corpus-wide error rate -- "
            "it is the rate among the rows most likely to be wrong. That makes "
            "the test asymmetric, and usefully so: a low rate on the hardest "
            "cases is strong evidence the headline holds, while a high rate is "
            "conclusive that it does not. A middling rate bounds the error "
            "from above and settles nothing."
        ),
        "verdict": _hand_check_verdict(fn_rate, fn_denom, fp_rate, fp_denom),
    }


def _provenance() -> dict:
    """Who filled the truth column, and how much that verdict is therefore worth.

    The queue was built to be filled by a human, because a second model reading
    the same kind of page is not independent of the first. When it is filled by
    the agent instead, the result is still evidence -- it is read off vendor
    pricing pages rather than off developer docs, which is the whole difference
    the test turns on -- but it is weaker evidence, and the file has to say so
    rather than let the reader assume otherwise.
    """
    meta_path = settings.data_dir / "hand_check_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if isinstance(meta.get("filled_by"), dict):
            return meta["filled_by"]
    return {
        "who": "unrecorded",
        "caveat": ("hand_check_meta.json does not record who filled the truth "
                   "column, so the independence of this check is unknown"),
    }


def _project(fn_rate: float | None, corpus_rows: list[dict] | None) -> dict:
    """What the self-serve share becomes if the measured miss rate generalised.

    An extrapolation from an enriched sample, so it is a worst case rather than
    an estimate, and is labelled as one.
    """
    if fn_rate is None or not corpus_rows:
        return {"available": False,
                "reason": "needs both a scored queue and the corpus rows"}
    n = len(corpus_rows)
    self_serve = sum(1 for r in corpus_rows if r["access_tier"] in SELF_SERVE)
    observed = round(self_serve / n, 4) if n else None
    corrected = round((self_serve * (1 - fn_rate)) / n, 4) if n else None
    return {
        "available": True,
        "corpus_rows": n,
        "observed_self_serve_share": observed,
        "worst_case_self_serve_share": corrected,
        "caveat": (
            "Applies the hand-check miss rate to every self-serve row in the "
            "corpus. The queue over-samples hard cases, so the true share sits "
            "between the worst case and the observed figure, not at the worst "
            "case. Quote it as a floor, never as a measurement."
        ),
    }


def _hand_check_verdict(fn_rate, fn_checked, fp_rate, fp_checked) -> str:
    if fn_rate is None:
        return ("no self-serve rows have been checked against a vendor page "
                "yet -- the ~90% self-serve figure is unverified and must not "
                "be quoted as a finding")
    if fn_checked < _MIN_TO_CONCLUDE:
        return (f"only {fn_checked} self-serve rows checked, below the "
                f"{_MIN_TO_CONCLUDE} needed to conclude; the figure remains "
                "unverified rather than confirmed")

    if fn_rate >= _FN_SYSTEMATIC:
        head = (f"SYSTEMATIC BIAS. The vendor's own pages contradict the "
                f"pipeline on {fn_rate:.0%} of the self-serve rows checked. "
                "The ~90% self-serve figure does not survive; it is an "
                "artefact of the extractor reading documented API as "
                "obtainable credentials, and it should be withdrawn as a "
                "finding rather than caveated")
    elif fn_rate >= _FN_INFLATED:
        head = (f"PARTIALLY REAL. {fn_rate:.0%} of checked self-serve rows are "
                "gated on the vendor's page. The corpus does skew self-serve, "
                "but the headline number is inflated by extraction and should "
                "be reported as a range with the miss rate attached, never as "
                "~90%")
    elif fn_rate <= _FN_SURVIVES:
        head = (f"SURVIVES. Only {fn_rate:.0%} of checked self-serve rows are "
                "gated on the vendor's page, and the queue was enriched with "
                "the hardest cases, so the true rate is no higher. The ~90% "
                "self-serve figure reads as a property of the corpus rather "
                "than of the extractor")
    else:
        head = (f"UNRESOLVED. A {fn_rate:.0%} miss rate is too high to call the "
                "figure clean and too low to call it an artefact; it bounds "
                "the error from above without settling the question")

    if fp_rate:
        head += (f". Separately, {fp_rate:.0%} of the {fp_checked} gated rows "
                 "are self-serve in fact, so the gating verdicts are not "
                 "clean either")
    return head
