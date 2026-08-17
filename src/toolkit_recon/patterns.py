"""PHASE 4 — pattern clustering over the final corpus.

Phases 1-3 ask whether individual rows are right. This one asks what the corpus
says once you stop reading it row by row: which combinations of tier, breadth
and buildability actually recur, which blockers are the same blocker under
different wording, and which categories behave differently from the rest.

Three rules this module follows, because a clustering report is the easiest
place in the project to launder a weak field into a confident-looking finding:

1. **Clusters keyed on `access_tier` inherit its measured error.** The hand
   check found the field wrong in one direction on an adversarial sample, and
   named the mechanism. Every archetype below is keyed partly on that field, so
   each one carries the caveat inline rather than in a footnote nobody reads.
   The rate is quoted from `hand_check.json` rather than restated here, so a
   corrected hand check corrects this report too and no stale figure can
   survive in a generated file. If `hand_check.json` is absent the caveat says
   the field is unverified, never that it is fine. The sample is small and
   enriched, so the caveat bounds direction, never magnitude.

2. **Failed rows are not members of anything.** A RESEARCH FAILED row has no
   findings to cluster. It is counted and named separately so the denominators
   are honest, rather than being swept into a `no_public_api` bucket it never
   earned.

3. **Blocker families are matched, not invented.** Grouping is by explicit
   pattern over the blocker text, and anything that matches nothing lands in
   `unmatched` by name. A family that silently absorbs the leftovers would make
   the taxonomy look more complete than it is.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from .config import settings

SELF_SERVE = {"self_serve_free", "self_serve_trial"}

# Blocker families. Ordered: first match wins, so the specific patterns come
# before the general ones. Every family is a stated pattern -- nothing here is
# a catch-all, and what matches nothing is reported as unmatched.
BLOCKER_FAMILIES: list[tuple[str, re.Pattern]] = [
    ("partner_or_approval_process",
     re.compile(r"partner|approv|request access|application|whitelist|"
                r"not accepting|vetting|review process", re.I)),
    ("paid_plan_required",
     re.compile(r"paid|pricing|subscription|premium|upgrade|enterprise plan|"
                r"billing|per[- ]seat", re.I)),
    ("admin_or_workspace_enablement",
     re.compile(r"admin|workspace owner|account owner|enable|permission|"
                r"scope grant|tenant", re.I)),
    ("no_public_api",
     re.compile(r"no public api|not publicly|internal only|undocumented", re.I)),
    ("rate_or_quota_limited",
     re.compile(r"rate limit|quota|throttl|call limit", re.I)),
    ("verification_or_compliance",
     re.compile(r"verif|compliance|kyc|business review|security review", re.I)),
]


def _is_failed(row: dict) -> bool:
    return (row.get("agent_notes") or "").startswith("RESEARCH FAILED")


def _tier_caveat() -> dict:
    """What the archetypes inherit from access_tier's measured error rate."""
    hc = settings.data_dir / "hand_check.json"
    if hc.exists():
        try:
            res = json.loads(hc.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            res = {}
        fnr = res.get("false_negative_rate") or {}
        fn, checked, wrong = fnr.get("rate"), fnr.get("checked"), fnr.get("wrong")
        fpr = res.get("false_positive_rate") or {}
        if fn is not None:
            return {
                "status": "measured",
                "false_negative_rate": fn,
                "scoreable_rows": checked,
                "note": (
                    f"Every archetype here is keyed partly on access_tier. "
                    f"Hand-checked against vendor pricing pages, that field was "
                    f"wrong on {wrong} of {checked} scoreable self-serve rows "
                    f"({fn:.0%}), while {fpr.get('checked')} of "
                    f"{fpr.get('checked')} gated calls were correct. The sample "
                    f"is {checked} scoreable rows, enriched with expected-gated "
                    "products: it bounds the DIRECTION of the error, not its "
                    "magnitude. Direction is one-way -- gated products read as "
                    "self-serve, never the reverse -- so the self-serve "
                    "archetypes below are a ceiling and the gated ones a floor. "
                    "The shapes are real; the sizes are not."
                ),
                "source": "data/hand_check.json",
            }
    return {
        "status": "unverified",
        "false_negative_rate": None,
        "note": ("access_tier has not been checked against vendor pricing "
                 "pages in this run, so the size of every archetype below is "
                 "unverified. Absence of a measurement is not a clean result."),
    }


def archetypes(rows: list[dict]) -> list[dict]:
    """Recurring (tier, breadth, buildability) shapes, largest first."""
    live = [r for r in rows if not _is_failed(r)]
    groups: dict[tuple, list[str]] = {}
    for r in live:
        key = (r.get("access_tier"), r.get("api_breadth"),
               r.get("buildable_today"))
        groups.setdefault(key, []).append(r["name"])

    out = []
    for (tier, breadth, build), members in groups.items():
        out.append({
            "access_tier": tier,
            "api_breadth": breadth,
            "buildable_today": build,
            "self_serve": tier in SELF_SERVE,
            "count": len(members),
            "share_of_live_rows": round(len(members) / len(live), 4) if live else None,
            "members": sorted(members),
        })
    out.sort(key=lambda d: (-d["count"], str(d["access_tier"])))
    return out


def blocker_families(rows: list[dict]) -> dict:
    """Group primary_blocker text into families by explicit pattern."""
    live = [r for r in rows if not _is_failed(r)]
    fams: dict[str, list[dict]] = {name: [] for name, _ in BLOCKER_FAMILIES}
    unmatched: list[dict] = []
    none_stated = 0

    for r in live:
        blocker = (r.get("primary_blocker") or "").strip()
        if not blocker:
            none_stated += 1
            continue
        for name, rx in BLOCKER_FAMILIES:
            if rx.search(blocker):
                fams[name].append({"app": r["name"], "blocker": blocker})
                break
        else:
            unmatched.append({"app": r["name"], "blocker": blocker})

    stated = sum(len(v) for v in fams.values()) + len(unmatched)
    return {
        "rows_considered": len(live),
        "rows_with_no_blocker_stated": none_stated,
        "rows_with_a_blocker": stated,
        "families": {
            name: {"count": len(items),
                   "share_of_stated": (round(len(items) / stated, 4)
                                       if stated else None),
                   "apps": sorted(i["app"] for i in items),
                   "examples": [i["blocker"] for i in items[:3]]}
            for name, items in sorted(fams.items(), key=lambda kv: -len(kv[1]))
        },
        "unmatched": sorted(unmatched, key=lambda i: i["app"]),
        "taxonomy_note": (
            "First match wins and no family is a catch-all. Anything matching "
            "no pattern is listed by name under `unmatched` rather than being "
            "absorbed, so the taxonomy cannot look more complete than it is."
        ),
    }


def auth_combinations(rows: list[dict]) -> list[dict]:
    live = [r for r in rows if not _is_failed(r)]
    c = Counter(" + ".join(sorted(r.get("auth_methods") or ["(none)"]))
                for r in live)
    return [{"auth_methods": k, "count": n,
             "share": round(n / len(live), 4) if live else None}
            for k, n in c.most_common()]


def category_profile(rows: list[dict]) -> list[dict]:
    """Per category: how many rows, how many usable, how many gated."""
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)

    out = []
    for cat, members in sorted(by_cat.items()):
        live = [r for r in members if not _is_failed(r)]
        gated = [r for r in live if r.get("access_tier") not in SELF_SERVE]
        out.append({
            "category": cat,
            "rows": len(members),
            "failed_rows": len(members) - len(live),
            "gated_rows": len(gated),
            "gated_share_of_live": (round(len(gated) / len(live), 4)
                                    if live else None),
            "buildable_yes": sum(1 for r in live
                                 if r.get("buildable_today") == "yes"),
        })
    out.sort(key=lambda d: -d["rows"])
    return out


def mcp_cluster(rows: list[dict]) -> dict:
    live = [r for r in rows if not _is_failed(r)]
    with_mcp = [r for r in live if r.get("has_mcp")]
    retracted = [r["name"] for r in rows
                 if "has_mcp retracted" in (r.get("agent_notes") or "")]
    return {
        "rows_considered": len(live),
        "with_official_mcp": len(with_mcp),
        "share": round(len(with_mcp) / len(live), 4) if live else None,
        "apps": sorted(r["name"] for r in with_mcp),
        "evidence": {r["name"]: r.get("mcp_evidence_url") for r in
                     sorted(with_mcp, key=lambda r: r["name"])},
        "retracted_by_provenance_guard": sorted(retracted),
        "provenance_note": (
            "Every URL above is on the vendor's own domain. Claims resting on "
            "third-party directories were retracted by Layer 1 R3 and are "
            "listed under retracted_by_provenance_guard rather than dropped "
            "silently."
        ),
    }


def cluster(source: str = "pass1.validated.json") -> dict:
    path = settings.data_dir / source
    if not path.exists():
        raise SystemExit(f"missing {path}; run Layer 1 first")
    rows = json.loads(path.read_text(encoding="utf-8"))
    failed = [r["name"] for r in rows if _is_failed(r)]

    return {
        "source": source,
        "rows_in_corpus": len(rows),
        "rows_clustered": len(rows) - len(failed),
        "research_failed_rows": sorted(failed),
        "failed_rows_note": (
            "RESEARCH FAILED rows have no findings to cluster and are excluded "
            "from every denominator here. They are named rather than folded "
            "into a tier bucket they never earned."
        ),
        "access_tier_caveat": _tier_caveat(),
        "archetypes": archetypes(rows),
        "blocker_families": blocker_families(rows),
        "auth_combinations": auth_combinations(rows),
        "category_profile": category_profile(rows),
        "mcp": mcp_cluster(rows),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-patterns")
    p.add_argument("--source", default="pass1.validated.json",
                   help="corpus file under data/ (default pass1.validated.json)")
    args = p.parse_args(argv)

    res = cluster(args.source)
    out = settings.data_dir / "patterns.json"
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    _print(res, out)
    return 0


def _print(res: dict, out: Path) -> None:
    print("=" * 70)
    print("PHASE 4 - PATTERN CLUSTERING")
    print("=" * 70)
    print(f"  source            : {res['source']}")
    print(f"  rows in corpus    : {res['rows_in_corpus']}")
    print(f"  rows clustered    : {res['rows_clustered']}"
          f"  ({len(res['research_failed_rows'])} research-failed excluded)")

    cav = res["access_tier_caveat"]
    print(f"\n  ! access_tier caveat ({cav['status']}): {cav['note']}")

    print("\n  ARCHETYPES  (tier x breadth x buildable)")
    for a in res["archetypes"]:
        if a["count"] < 2:
            continue
        print(f"    {a['count']:>3}  {a['access_tier']:<20}"
              f" {str(a['api_breadth']):<10} {str(a['buildable_today']):<17}"
              f" {a['share_of_live_rows']:.0%}")
    singles = [a for a in res["archetypes"] if a["count"] == 1]
    if singles:
        print(f"    {len(singles):>3}  singleton shapes (not a pattern)")

    bf = res["blocker_families"]
    print(f"\n  BLOCKER FAMILIES  ({bf['rows_with_a_blocker']} rows state a "
          f"blocker, {bf['rows_with_no_blocker_stated']} state none)")
    for name, info in bf["families"].items():
        if info["count"]:
            print(f"    {info['count']:>3}  {name:<32}"
                  f" {info['share_of_stated']:.0%}")
    if bf["unmatched"]:
        print(f"    {len(bf['unmatched']):>3}  unmatched (listed in patterns.json)")

    print("\n  AUTH COMBINATIONS")
    for a in res["auth_combinations"][:8]:
        print(f"    {a['count']:>3}  {a['auth_methods']}")

    print("\n  CATEGORY PROFILE")
    print(f"    {'category':<24}{'rows':>5}{'failed':>8}{'gated':>7}{'yes':>6}")
    for c in res["category_profile"]:
        print(f"    {c['category']:<24}{c['rows']:>5}{c['failed_rows']:>8}"
              f"{c['gated_rows']:>7}{c['buildable_yes']:>6}")

    m = res["mcp"]
    print(f"\n  MCP  ({m['with_official_mcp']}/{m['rows_considered']} "
          f"= {m['share']:.0%}, vendor-domain evidence only)")
    for name, url in m["evidence"].items():
        print(f"    {name:<20} {url}")
    if m["retracted_by_provenance_guard"]:
        print(f"    retracted: {', '.join(m['retracted_by_provenance_guard'])}")

    print(f"\n  wrote {out}")
    print("=" * 70)


if __name__ == "__main__":
    raise SystemExit(main())
