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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-report")
    p.add_argument("--pass-number", type=int, default=1)
    p.add_argument("--triage", action="store_true", help="print the human review queue")
    p.add_argument("--delta", nargs=2, type=int, metavar=("A", "B"),
                   help="compare two passes, e.g. --delta 1 2")
    args = p.parse_args(argv)

    if args.delta:
        delta(*args.delta)
        return 0

    rows, traces = _load(args.pass_number)
    if args.triage:
        triage(rows, traces)
    else:
        summarise(rows, traces)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
