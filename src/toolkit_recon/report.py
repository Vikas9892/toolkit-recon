"""Post-run analysis over pass{N}.json + logs/trace.jsonl.

Answers the questions a reviewer actually asks: how much of this is
trustworthy, which rows need a human, and did the pipeline's own machinery
(ranking, caching, the token governor) do its job?

    python -m toolkit_recon.report --pass-number 1
    python -m toolkit_recon.report --triage      # rows a human should review
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .config import settings


def _load(pass_number: int) -> tuple[list[dict], list[dict]]:
    rows_path = settings.data_dir / f"pass{pass_number}.json"
    if not rows_path.exists():
        raise SystemExit(f"missing {rows_path}; run the pipeline first")
    rows = json.loads(rows_path.read_text(encoding="utf-8"))

    trace_path = settings.logs_dir / "trace.jsonl"
    traces: list[dict] = []
    if trace_path.exists():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    traces.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    # Keep the last trace per app, so a resumed run reports its final attempt.
    latest = {t["slug"]: t for t in traces if t.get("pass_number") == pass_number}
    return rows, list(latest.values())


def _snapshot(pass_number: int) -> Path:
    """Materialise pass{N}.json from the checkpoint, in corpus order.

    The pipeline only writes its output file at the end of a run. A run that is
    still going -- or that was killed -- leaves results in the checkpoint only,
    which cannot be committed or inspected as a deliverable. This closes that
    gap without touching the pipeline.
    """
    from .apps import APPS

    ckpt = settings.checkpoint_dir / f"pass{pass_number}.checkpoint.json"
    if not ckpt.exists():
        raise SystemExit(f"missing {ckpt}")
    done = json.loads(ckpt.read_text(encoding="utf-8"))
    rows = [done[a.slug] for a in APPS if a.slug in done]
    out = settings.data_dir / f"pass{pass_number}.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _bar(n: int, total: int, width: int = 34) -> str:
    if not total:
        return ""
    return "#" * int(width * n / total)


def _pct(n: int, total: int) -> str:
    return f"{(100 * n / total):5.1f}%" if total else "  0.0%"


def summarise(rows: list[dict], traces: list[dict]) -> None:
    total = len(rows)
    conf = Counter(r["confidence"] for r in rows)
    failures = [r for r in rows if r["agent_notes"].startswith("RESEARCH FAILED")]

    print("=" * 66)
    print(f"PASS {rows[0]['pass_number'] if rows else '?'} REPORT   ({total} rows)")
    if total < 100:
        # Percentages below are over N, not over the 100-app corpus. Saying so
        # once at the top is cheaper than a reader assuming the denominator.
        print(f"PARTIAL CORPUS: {total} of 100 apps profiled. "
              f"All percentages are over {total}.")
    print("=" * 66)

    print("\nCONFIDENCE")
    for lvl in ("high", "medium", "low"):
        n = conf.get(lvl, 0)
        print(f"  {lvl:<7}{n:>4}  {_pct(n, total)}  {_bar(n, total)}")
    trusted = conf.get("high", 0) + conf.get("medium", 0)
    print(f"\n  usable without review (high)      : {conf.get('high', 0)}")
    print(f"  needs a look (medium)             : {conf.get('medium', 0)}")
    print(f"  needs rework (low)                : {conf.get('low', 0)}")
    print(f"  high+medium share                 : {_pct(trusted, total)}")

    print("\nBUILDABILITY")
    build = Counter(r["buildable_today"] for r in rows)
    for k in ("yes", "yes_with_caveats", "no"):
        print(f"  {k:<18}{build.get(k, 0):>4}  {_bar(build.get(k, 0), total)}")

    print("\nACCESS TIER")
    for k, n in Counter(r["access_tier"] for r in rows).most_common():
        print(f"  {k:<22}{n:>4}  {_bar(n, total)}")

    print("\nAUTH METHODS (rows may list several)")
    auth = Counter(a for r in rows for a in r["auth_methods"])
    for k, n in auth.most_common():
        print(f"  {k:<22}{n:>4}  {_bar(n, total)}")

    print("\nAPI STYLE")
    for k, n in Counter(s for r in rows for s in r["api_style"]).most_common():
        print(f"  {k:<22}{n:>4}  {_bar(n, total)}")

    print("\nAPI BREADTH")
    for k, n in Counter(r["api_breadth"] for r in rows).most_common():
        print(f"  {k:<22}{n:>4}  {_bar(n, total)}")

    mcp = [r for r in rows if r["has_mcp"]]
    print(f"\nMCP\n  official MCP server evidenced   : {len(mcp)}")
    for r in mcp[:12]:
        print(f"    - {r['name']}: {r['mcp_evidence_url']}")

    print("\nTOP BLOCKERS")
    blockers = Counter(
        (r["primary_blocker"] or "").strip().lower()
        for r in rows
        if r.get("primary_blocker")
    )
    for k, n in blockers.most_common(10):
        print(f"  {n:>3}x  {k[:70]}")

    # ---- process metrics, from the trace ----
    if traces:
        print("\n" + "-" * 66)
        print("PROCESS METRICS (from logs/trace.jsonl)")
        print("-" * 66)
        toks = sum(t.get("total_tokens", 0) for t in traces)
        wall = sum(t.get("wall_time_s", 0.0) for t in traces)
        hits = sum(t.get("cache_hits", 0) for t in traces)
        misses = sum(t.get("cache_misses", 0) for t in traces)
        fetched = sum(len(t.get("urls_fetched", [])) for t in traces)
        failed = sum(len(t.get("urls_failed", [])) for t in traces)
        official = sum(1 for t in traces if t.get("official_domains_reached"))
        n = len(traces)

        print(f"  apps traced                     : {n}")
        print(f"  LLM tokens (total / mean)       : {toks:,} / {toks // max(1, n):,}")
        print(f"  documents fetched OK            : {fetched}")
        print(f"  document fetch failures         : {failed}")
        print(
            f"  fetch cache hit rate            : {_pct(hits, hits + misses)}"
            f"  ({hits}/{hits + misses})"
        )
        print(
            f"  reached official vendor domain  : {official}/{n}  {_pct(official, n)}"
        )
        print(f"  mean wall time per app          : {wall / max(1, n):.1f}s")

        errs = Counter(
            (t.get("error") or "").split(":")[0] for t in traces if t.get("error")
        )
        if errs:
            print("\n  failure modes")
            for k, c in errs.most_common():
                print(f"    {c:>3}x  {k[:60]}")

    print(f"\nFAILURES: {len(failures)}")
    for r in failures:
        print(f"  - {r['name']}: {r['agent_notes'][:100]}")
    print("=" * 66)



# ---------------------------------------------------------------------------
# Findings view: the cross-cuts a reviewer asks for
# ---------------------------------------------------------------------------

# Blockers are free text, so group them by the thing that actually gates access
# rather than by exact wording. Order matters: the first match wins, and the
# harder gate should win when a blocker mentions several.
BLOCKER_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("partner / commercial agreement",
     ("partner", "commercial agreement", "reseller", "contract", "sales team",
      "contact sales", "business review")),
    ("approval / application required",
     ("approval", "approved", "apply", "application", "review process",
      "request access", "allowlist", "whitelist", "waitlist")),
    ("admin / workspace enablement",
     ("admin", "workspace owner", "tenant", "enable", "provision",
      "super user", "instance")),
    ("paid plan required",
     ("paid", "enterprise", "premium", "pro plan", "subscription", "upgrade",
      "billing", "higher tier", "plus plan")),
    ("no public API", ("no public api", "not public", "undocumented",
                       "no documented", "internal api")),
    ("scope / permission limits", ("scope", "permission", "read-only",
                                   "write access", "granular")),
    ("rate limits / quota", ("rate limit", "quota", "throttl")),
    ("auth complexity", ("oauth", "jwt", "signature", "hmac", "certificate",
                         "mutual tls")),
]


def group_blocker(text: str) -> str:
    low = (text or "").lower()
    for label, needles in BLOCKER_GROUPS:
        if any(n in low for n in needles):
            return label
    return "other / unclassified"


def findings(rows: list[dict], traces: list[dict]) -> None:
    total = len(rows)
    print("=" * 74)
    print(f"FINDINGS  —  {total} of 100 apps profiled")
    if total < 100:
        print(f"PARTIAL CORPUS. Every figure below is over {total}, not 100.")
        # State the reason only when the artifacts show it. Asserting a cause
        # the data does not evidence is the same error this project keeps
        # guarding against, just pointed at the reader instead of the schema.
        quota = [t for t in traces
                 if "daily token budget" in (t.get("error") or "").lower()]
        if quota:
            print(f"Reason: provider daily token budget exhausted "
                  f"({len(quota)} row(s) hit it).")
        elif (settings.data_dir / "queue_order.json").exists():
            print("Run incomplete. The queue was reordered for category "
                  "coverage (data/queue_order.json).")
        else:
            print("Run incomplete; reason not recorded in the trace.")
    print("=" * 74)

    # ---- coverage ----
    cats = Counter(r["category"] for r in rows)
    print("\nPER-CATEGORY COVERAGE")
    for c, n in sorted(cats.items()):
        flag = "" if n >= 3 else "   <-- too thin for a per-category claim"
        print(f"  {c:<24}{n:>3}  {_bar(n, max(cats.values()), 20)}{flag}")
    print(f"  categories represented   : {len(cats)}/14")
    print(f"  categories with >=3 rows : {sum(1 for n in cats.values() if n >= 3)}/14")

    # ---- confidence ----
    conf = Counter(r["confidence"] for r in rows)
    print("\nCONFIDENCE")
    for lvl in ("high", "medium", "low"):
        n = conf.get(lvl, 0)
        print(f"  {lvl:<8}{n:>3}  {_pct(n, total)}  {_bar(n, total)}")

    # ---- tier x breadth ----
    print("\nACCESS TIER x API BREADTH")
    tiers = [t for t, _ in Counter(r["access_tier"] for r in rows).most_common()]
    breadths = ["narrow", "moderate", "broad"]
    print(f"  {'':<22}" + "".join(f"{b:>10}" for b in breadths) + f"{'total':>8}")
    for t in tiers:
        cells = [sum(1 for r in rows
                     if r["access_tier"] == t and r["api_breadth"] == b)
                 for b in breadths]
        print(f"  {t:<22}" + "".join(f"{c:>10}" for c in cells)
              + f"{sum(cells):>8}")
    col = [sum(1 for r in rows if r["api_breadth"] == b) for b in breadths]
    print(f"  {'total':<22}" + "".join(f"{c:>10}" for c in col) + f"{total:>8}")

    # ---- tier by confidence: is the distribution real? ----
    from .tier_audit import pricing_evidence, tier_by_confidence

    tc = tier_by_confidence(rows)
    print("\nACCESS TIER x CONFIDENCE")
    print(f"  {'':<22}{'high':>12}{'medium/low':>14}")
    print(f"  {'cohort size':<22}{tc['high_cohort_n']:>12}"
          f"{tc['weak_cohort_n']:>14}")
    for t in ["self_serve_free", "self_serve_trial", "paid_plan_required",
              "admin_approval", "partner_gated", "no_public_api"]:
        h = tc["tier_distribution_high"][t]
        w = tc["tier_distribution_weak"][t]
        if h or w:
            print(f"  {t:<22}{h:>12}{w:>14}")
    sh, sw = tc["self_serve_share_high"], tc["self_serve_share_weak"]
    print(f"  {'self-serve share':<22}"
          f"{(f'{sh:.0%}' if sh is not None else 'n/a'):>12}"
          f"{(f'{sw:.0%}' if sw is not None else 'n/a'):>14}")
    if tc["share_gap_weak_minus_high"] is not None:
        print(f"  gap (weak - high)     : {tc['share_gap_weak_minus_high']:+.0%}")
    print(f"  -> {tc['verdict']}")

    # ---- did the evidence ever discuss access at all? ----
    pe = pricing_evidence(rows)
    print("\nSELF-SERVE ROWS WITH NO PRICING EVIDENCE")
    print(f"  self-serve rows              : {pe['self_serve_rows']}")
    print(f"  evidence mentioned access    : {pe['with_pricing_language']}")
    print(f"  evidence NEVER mentioned it  : {pe['without_any_pricing_language']}"
          + (f"  ({pe['share_without']:.0%})" if pe["share_without"] is not None
             else ""))
    for row in pe["rows_without_pricing_evidence"][:15]:
        print(f"    - {row['app']:<26} {row['tier']:<18} {row['confidence']}")
    print(f"  -> {pe['verdict']}")
    print(f"  schema note: {pe['schema_note']}")

    # ---- buildability ----
    print("\nBUILDABLE TODAY")
    build = Counter(r["buildable_today"] for r in rows)
    for k in ("yes", "yes_with_caveats", "no"):
        print(f"  {k:<20}{build.get(k, 0):>3}  {_pct(build.get(k, 0), total)}"
              f"  {_bar(build.get(k, 0), total)}")

    # ---- blockers ----
    blocked = [r for r in rows if (r.get("primary_blocker") or "").strip()]
    groups = Counter(group_blocker(r["primary_blocker"]) for r in blocked)
    print(f"\nBLOCKERS, GROUPED  ({len(blocked)} of {total} rows name one)")
    for g, n in groups.most_common():
        print(f"  {n:>3}  {g}")

    # ---- MCP ----
    mcp = [r for r in rows if r.get("has_mcp")]
    print(f"\nSHIPS AN OFFICIAL MCP SERVER  ({len(mcp)} of {total})")
    for r in sorted(mcp, key=lambda r: r["name"]):
        print(f"  - {r['name']:<26} {r.get('mcp_evidence_url') or ''}")
    if not mcp:
        print("  none evidenced in the pages fetched "
              "(absence of evidence, not evidence of absence)")

    # ---- extractor split ----
    models = Counter(r.get("extracted_by") or "unrecorded" for r in rows)
    if len(models) > 1:
        print("\nEXTRACTOR SPLIT")
        for m, n in models.most_common():
            print(f"  {m:<28}{n:>3}")

    # ---- deadline clustering ----
    hits = [t for t in traces if t.get("deadline_hit")]
    print(f"\nDEADLINE TIMEOUTS  ({len(hits)})")
    if hits:
        by_stage = Counter(t.get("stage", "?") for t in hits)
        by_load = Counter(t.get("workers_in_flight", 0) for t in hits)
        for st, n in by_stage.most_common():
            print(f"  stage {st:<12}{n:>3}")
        print("  by concurrent workers: " + ", ".join(
            f"{k}->{v}" for k, v in sorted(by_load.items())))
        for t in hits:
            print(f"    {t['name']:<24} stage={t.get('stage')} "
                  f"{t.get('wall_time_s')}s docs={len(t.get('urls_fetched', []))}")
    else:
        print("  none")

    # ---- failures ----
    fails = [r for r in rows if r["agent_notes"].startswith("RESEARCH FAILED")]
    print(f"\nFAILED ROWS  ({len(fails)})")
    for r in fails:
        print(f"  - {r['name']}: {r['agent_notes'][:96]}")
    print("=" * 74)

def triage(rows: list[dict], traces: list[dict]) -> None:
    """The point of the confidence column: a concrete human review queue."""
    by_name = {t.get("name"): t for t in traces}
    order = {"low": 0, "medium": 1, "high": 2}
    queue = sorted(
        (r for r in rows if r["confidence"] != "high"),
        key=lambda r: (order[r["confidence"]], r["name"]),
    )
    print(f"HUMAN REVIEW QUEUE - {len(queue)} of {len(rows)} rows\n")
    for r in queue:
        why = by_name.get(r["name"], {}).get("confidence_reason", "")
        print(f"[{r['confidence']:<6}] {r['name']}  ({r['category']})")
        print(f"    why      : {why}")
        print(f"    tier     : {r['access_tier']}   auth: {', '.join(r['auth_methods'])}")
        if r.get("primary_blocker"):
            print(f"    blocker  : {r['primary_blocker']}")
        for u in r["evidence_urls"][:3]:
            print(f"    evidence : {u}")
        print()


GRADED = ["one_liner", "auth_methods", "access_tier", "api_style", "api_breadth",
          "has_mcp", "buildable_today", "confidence"]


# ---------------------------------------------------------------------------
# Phase 3 audit scoring
# ---------------------------------------------------------------------------

# CSV verdict column -> the schema field(s) it adjudicates.
VERDICT_FIELDS = {
    "verdict_auth": "auth_methods",
    "verdict_tier": "access_tier",
    "verdict_api": "api_style/api_breadth",
    "verdict_mcp": "has_mcp",
    "verdict_buildable": "buildable_today",
}
VERDICT_VOCAB = {"correct", "partially_correct", "wrong", "unverifiable"}


def _tally() -> dict[str, int]:
    return {v: 0 for v in VERDICT_VOCAB}


def _precision(t: dict[str, int]) -> dict:
    """Strict and lenient precision over verifiable verdicts only.

    `unverifiable` is excluded from the denominator rather than counted as a
    miss: a field no public document can settle is a fact about the vendor's
    documentation, not an error by the agent. It is reported separately so the
    exclusion is visible instead of flattering.
    """
    scored = t["correct"] + t["partially_correct"] + t["wrong"]
    if not scored:
        return {**t, "scored": 0, "precision": None, "precision_lenient": None}
    return {
        **t,
        "scored": scored,
        "precision": round(t["correct"] / scored, 4),
        "precision_lenient": round(
            (t["correct"] + 0.5 * t["partially_correct"]) / scored, 4
        ),
    }


def score_audit(csv_path: Path) -> dict:
    """Read the completed audit CSV and produce the ground-truth report."""
    import csv as _csv

    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(_csv.DictReader(fh))

    by_field: dict[str, dict[str, int]] = {f: _tally() for f in VERDICT_FIELDS.values()}
    by_conf: dict[str, dict[str, int]] = {}
    misses: list[dict] = []
    unfilled: list[str] = []
    bad_vocab: list[dict] = []
    unverifiable = 0
    audited_rows = 0

    for r in rows:
        name = (r.get("name") or "").strip()
        conf = (r.get("agent_confidence") or "").strip() or "unknown"
        # high vs medium/low is the comparison that tells us whether the
        # confidence signal carries information at all.
        bucket = "high" if conf == "high" else "medium_low"
        by_conf.setdefault(bucket, _tally())

        filled = False
        for col, field in VERDICT_FIELDS.items():
            v = (r.get(col) or "").strip().lower()
            if not v:
                continue
            if v not in VERDICT_VOCAB:
                bad_vocab.append({"app": name, "column": col, "value": v})
                continue
            filled = True
            by_field[field][v] += 1
            by_conf[bucket][v] += 1
            if v == "unverifiable":
                unverifiable += 1
            elif v in ("wrong", "partially_correct"):
                misses.append({
                    "app": name,
                    "field": field,
                    "verdict": v,
                    "agent_confidence": conf,
                    "agent_said": r.get(_agent_col(field), ""),
                    "truth": (r.get("truth_notes") or "").strip(),
                    "why_it_failed": (r.get("why_it_failed") or "").strip(),
                })
        if filled:
            audited_rows += 1
        else:
            unfilled.append(name)

    return {
        "source_csv": str(csv_path),
        "rows_in_queue": len(rows),
        "rows_audited": audited_rows,
        "rows_not_yet_audited": unfilled,
        "invalid_verdict_values": bad_vocab,
        "verdict_vocabulary": sorted(VERDICT_VOCAB),
        "precision_definition": (
            "precision = correct / (correct + partially_correct + wrong). "
            "unverifiable is excluded from the denominator and reported "
            "separately. precision_lenient credits partially_correct as 0.5."
        ),
        "precision_by_field": {f: _precision(t) for f, t in by_field.items()},
        "precision_by_confidence": {b: _precision(t) for b, t in by_conf.items()},
        "misses": misses,
        "unverifiable_count": unverifiable,
    }


def _agent_col(field: str) -> str:
    if field == "api_style/api_breadth":
        return "agent_api_style"
    return f"agent_{field}"


def delta(a: int, b: int) -> None:
    """Compare two passes: the accuracy delta `pass_number` exists for.

    Only rows present in both are compared, since a re-check pass deliberately
    covers a subset. Fields that move are the ones a single pass was not
    entitled to be sure about.
    """
    rows_a = {r["name"]: r for r in json.loads(
        (settings.data_dir / f"pass{a}.json").read_text(encoding="utf-8"))}
    rows_b = {r["name"]: r for r in json.loads(
        (settings.data_dir / f"pass{b}.json").read_text(encoding="utf-8"))}
    shared = sorted(set(rows_a) & set(rows_b))

    print(f"ACCURACY DELTA  pass{a} -> pass{b}   ({len(shared)} rows in both)\n")
    if not shared:
        return

    churn = Counter()
    upgraded = downgraded = 0
    changed_rows: list[tuple[str, list[str]]] = []

    order = {"low": 0, "medium": 1, "high": 2}
    for name in shared:
        ra, rb = rows_a[name], rows_b[name]
        diffs = []
        for f in GRADED:
            if f == "one_liner":
                continue
            va, vb = ra.get(f), rb.get(f)
            if isinstance(va, list):
                va, vb = sorted(va), sorted(vb)
            if va != vb:
                churn[f] += 1
                diffs.append(f"{f}: {va} -> {vb}")
        d = order[rb["confidence"]] - order[ra["confidence"]]
        upgraded += d > 0
        downgraded += d < 0
        if diffs:
            changed_rows.append((name, diffs))

    print(f"  rows with any change      : {len(changed_rows)}/{len(shared)}"
          f"  ({100 * len(changed_rows) / len(shared):.0f}%)")
    print(f"  confidence upgraded       : {upgraded}")
    print(f"  confidence downgraded     : {downgraded}")
    print("\n  fields that moved")
    for f, n in churn.most_common():
        print(f"    {f:<20}{n:>4}")

    print("\n  per-row changes")
    for name, diffs in changed_rows:
        print(f"    {name}")
        for d in diffs:
            print(f"      {d}")


def _rate(v) -> str:
    """Format a 0..1 rate. Distinct from _pct, which takes n/total."""
    return f"{v:.1%}" if isinstance(v, float) else "n/a"


def _print_audit(res: dict, out: Path) -> None:
    print("=" * 70)
    print("PHASE 3 â€” HUMAN AUDIT  (ground truth)")
    print("=" * 70)
    print(f"  rows in queue      : {res['rows_in_queue']}")
    print(f"  rows audited       : {res['rows_audited']}")
    if res["rows_not_yet_audited"]:
        n = len(res["rows_not_yet_audited"])
        print(f"  ! not yet audited  : {n} ({', '.join(res['rows_not_yet_audited'][:6])}"
              + (" ..." if n > 6 else "") + ")")
    if res["invalid_verdict_values"]:
        print(f"  ! invalid verdicts : {len(res['invalid_verdict_values'])} "
              f"(must be one of {', '.join(res['verdict_vocabulary'])})")

    print("\n  PRECISION BY CONFIDENCE  <- the headline: does confidence mean anything?")
    for bucket in ("high", "medium_low"):
        t = res["precision_by_confidence"].get(bucket)
        if not t:
            continue
        print(f"    {bucket:<12} precision {_rate(t['precision']):>7}"
              f"  (lenient {_rate(t['precision_lenient']):>7})"
              f"  n={t['scored']}  unverifiable={t['unverifiable']}")
    hi = res["precision_by_confidence"].get("high", {}).get("precision")
    lo = res["precision_by_confidence"].get("medium_low", {}).get("precision")
    if isinstance(hi, float) and isinstance(lo, float):
        gap = hi - lo
        verdict = ("confidence carries signal" if gap > 0.05
                   else "confidence does NOT separate correct from incorrect"
                   if gap <= 0 else "signal is weak")
        print(f"    -> gap {gap:+.1%}: {verdict}")

    print("\n  PRECISION BY FIELD")
    for f, t in sorted(res["precision_by_field"].items(),
                       key=lambda kv: (kv[1]["precision"] is None,
                                       kv[1]["precision"] or 0)):
        print(f"    {f:<22} {_rate(t['precision']):>7}  n={t['scored']:<3}"
              f" correct={t['correct']} partial={t['partially_correct']}"
              f" wrong={t['wrong']} unverifiable={t['unverifiable']}")

    print(f"\n  unverifiable fields : {res['unverifiable_count']}")
    print(f"  misses              : {len(res['misses'])}")
    for m in res["misses"][:12]:
        print(f"    [{m['agent_confidence']:<6}] {m['app']} / {m['field']}"
              f" â€” {m['verdict']}")
        if m["why_it_failed"]:
            print(f"        why: {m['why_it_failed'][:96]}")

    print(f"\n  wrote {out}")
    print("  Fold into the progression with: python -m toolkit_recon.progression")
    print("=" * 70)


def _print_hand_check(res: dict, out: Path) -> None:
    fn, fp = res["false_negative_rate"], res["false_positive_rate"]
    print("=" * 70)
    print("HAND CHECK - access_tier against the vendor's own page")
    print("=" * 70)
    print(f"  rows in queue      : {res['rows_in_queue']}")
    print(f"  rows scored        : {res['rows_scored']}")
    prov = res.get("filled_by") or {}
    print(f"  truth filled by    : {prov.get('who', 'unrecorded')}")
    if prov.get("caveat"):
        print(f"    ! {prov['caveat']}")
    if res["rows_not_yet_checked"]:
        n = len(res["rows_not_yet_checked"])
        print(f"  ! not yet checked  : {n} "
              f"({', '.join(res['rows_not_yet_checked'][:6])}"
              + (" ..." if n > 6 else "") + ")")
    if res["invalid_truth_values"]:
        print(f"  ! invalid truth    : {len(res['invalid_truth_values'])} "
              f"(must be one of {', '.join(res['truth_vocabulary'])})")
    if res["excluded_research_failures"]:
        print("  excluded (research failed, no tier claim to score): "
              + ", ".join(res["excluded_research_failures"]))

    print("\n  ERROR RATES  <- does the self-serve figure survive ground truth?")
    print(f"    false negative  {_rate(fn['rate']):>7}"
          f"  ({fn['wrong']}/{fn['checked']} called self-serve, actually gated)")
    print(f"    false positive  {_rate(fp['rate']):>7}"
          f"  ({fp['wrong']}/{fp['checked']} called gated, actually self-serve)")
    if res["same_class_mismatches"]:
        print(f"    wrong tier but right side of the line: "
              f"{res['same_class_mismatches']}")

    print("\n  PER-APP VERDICTS")
    for a in res["per_app"]:
        mark = " " if a["outcome"] == "correct" else "!"
        print(f"   {mark}{a['app']:<26} {a['agent_access_tier']:<18}"
              f" -> {a['truth_access_tier']:<18} {a['outcome']}")
        if a.get("vendor_evidence_url"):
            print(f"      evidence: {a['vendor_evidence_url']}")
        elif a.get("evidence_note"):
            print(f"      ! {a['evidence_note']}")
        if a.get("why_it_failed"):
            print(f"      why: {a['why_it_failed']}")

    gap = res.get("schema_cannot_express") or {}
    if gap.get("count"):
        print(f"\n  SCHEMA CANNOT EXPRESS  ({gap['count']}, excluded from both rates)")
        for g in gap["rows"]:
            print(f"    {g['app']:<26} pipeline said {g['agent_access_tier']}")
            if g.get("vendor_evidence_url"):
                print(f"      evidence: {g['vendor_evidence_url']}")
        print(f"    {gap['note']}")

    ap = res.get("agent_filled_pass_vs_human") or {}
    if ap.get("compared"):
        print(f"\n  AGENT-FILLED PASS vs HUMAN  "
              f"({ap['disagreed_with_human']} of {ap['compared']} wrong)")
        for d in ap["disagreements"]:
            print(f"    {d['app']:<26} agent said {d['agent_pass_said']:<20}"
                  f" human says {d['human_said']}")
        print(f"    {ap['finding']}")

    proj = res["corpus_projection"]
    if not proj.get("available") and proj.get("reason"):
        print(f"\n  CORPUS PROJECTION: suppressed. {proj['reason']}")
    if proj.get("available"):
        print("\n  CORPUS PROJECTION (worst case, not a measurement)")
        print(f"    observed self-serve share : "
              f"{proj['observed_self_serve_share']:.0%}"
              f"  over {proj['corpus_rows']} rows")
        print(f"    worst case if rate held   : "
              f"{proj['worst_case_self_serve_share']:.0%}")
        print(f"    {proj['caveat']}")

    print(f"\n  -> {res['verdict']}")
    print(f"\n  {res['why_this_test_and_not_the_cross_tab']}")
    print(f"\n  {res['sample_note']}")
    print(f"\n  wrote {out}")
    print("=" * 70)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-report")
    p.add_argument("--pass-number", type=int, default=1)
    p.add_argument("--triage", action="store_true", help="print the human review queue")
    p.add_argument("--findings", action="store_true",
                   help="coverage, cross-tabs, grouped blockers, MCP list, "
                        "deadline clustering")
    p.add_argument("--hand-check", action="store_true",
                   help="write data/hand_check_queue.csv for manual tier "
                        "verification against vendor pricing pages")
    p.add_argument("--force", action="store_true",
                   help="with --hand-check, rebuild the queue even though its "
                        "truth column has already been filled (discards it)")
    p.add_argument("--hand-check-score", nargs="?",
                   const="hand_check_queue.csv", default=None, metavar="CSV",
                   help="score the completed hand-check CSV against vendor "
                        "pages -> data/hand_check.json")
    p.add_argument("--delta", nargs=2, type=int, metavar=("A", "B"),
                   help="compare two passes, e.g. --delta 1 2")
    p.add_argument("--audit", nargs="?", const="audit_queue.csv", default=None,
                   metavar="CSV",
                   help="score a completed audit CSV -> data/human_audit.json")
    p.add_argument("--snapshot", action="store_true",
                   help="write pass{N}.json from the checkpoint without running "
                        "the pipeline, so partial results can be inspected and "
                        "committed while a run is still going")
    args = p.parse_args(argv)

    if args.snapshot:
        out = _snapshot(args.pass_number)
        print(f"wrote {out}")
        return 0

    if args.hand_check:
        from .tier_audit import HandCheckAlreadyFilled, hand_check_queue
        rows, _ = _load(args.pass_number)
        try:
            path, meta = hand_check_queue(rows, force=args.force)
        except HandCheckAlreadyFilled as e:
            raise SystemExit(str(e)) from e
        print(f"wrote {path}  ({meta['queue_size']} rows)")
        if meta["requested_but_not_in_corpus"]:
            print("\n  ! requested but NOT IN CORPUS, so not queued:")
            print("    " + ", ".join(meta["requested_but_not_in_corpus"]))
            print("    They were not silently replaced; see hand_check_meta.json.")
        for r in meta["rows"]:
            print(f"    {r['name']:<28} {r['why']}")
        return 0

    if args.hand_check_score:
        from .tier_audit import score_hand_check
        csv_path = Path(args.hand_check_score)
        if not csv_path.is_absolute() and not csv_path.exists():
            csv_path = settings.data_dir / args.hand_check_score
        if not csv_path.exists():
            raise SystemExit(
                f"missing {csv_path}; build it with "
                "`python -m toolkit_recon.report --hand-check`")
        # The projection needs the corpus; a missing pass file is not fatal,
        # it just means the projection block reports itself unavailable.
        try:
            corpus, _ = _load(args.pass_number)
        except SystemExit:
            corpus = None
        result = score_hand_check(csv_path, corpus)
        out = settings.data_dir / "hand_check.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        _print_hand_check(result, out)
        return 0

    if args.audit:
        csv_path = Path(args.audit)
        if not csv_path.is_absolute() and not csv_path.exists():
            csv_path = settings.data_dir / args.audit
        if not csv_path.exists():
            raise SystemExit(f"missing {csv_path}; build it with "
                             "`python -m toolkit_recon.audit`")
        result = score_audit(csv_path)
        out = settings.data_dir / "human_audit.json"
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        _print_audit(result, out)
        return 0

    if args.delta:
        delta(*args.delta)
        return 0

    rows, traces = _load(args.pass_number)
    if args.triage:
        triage(rows, traces)
    elif args.findings:
        findings(rows, traces)
    else:
        summarise(rows, traces)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

