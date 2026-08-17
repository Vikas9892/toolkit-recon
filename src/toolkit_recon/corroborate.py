"""LAYER 2 — corroboration between two independent passes.

The two passes differ in both search queries (`QUERY_SETS`) and extraction
framing (`SYSTEM_BY_PASS`). Pass 1 asks what the product offers; pass 2 asks
what will break when you try to ship it. Agreement between two runs reasoning
from opposite priors is a real signal; agreement between one run and a copy of
itself is not.

The promotion rule is deliberately code-side and conservative:

* every compared field agrees  -> promote one level (never above ``high``)
* any field disagrees          -> that field is disputed, row routes to Layer 3

Confidence is still never taken from a model. Promotion is earned by measured
inter-pass agreement, and a row that Layer 1 corrected can never be promoted
past the ceiling that correction implies.
"""

from __future__ import annotations

import argparse
import json

from .config import settings

LADDER = ["low", "medium", "high"]

# Fields worth comparing. Prose fields (one_liner, agent_notes) are excluded:
# two runs will always word them differently, and that difference means nothing.
COMPARED = [
    "auth_methods",
    "access_tier",
    "api_style",
    "api_breadth",
    "has_mcp",
    "buildable_today",
]
SET_FIELDS = {"auth_methods", "api_style"}


def _up(conf: str) -> str:
    return LADDER[min(len(LADDER) - 1, LADDER.index(conf) + 1)]


def _norm(field: str, value):
    if field in SET_FIELDS:
        return sorted(value or [])
    return value


def compare(row1: dict, row2: dict) -> tuple[list[str], list[dict]]:
    """Return (agreeing_fields, disagreements)."""
    agree: list[str] = []
    disputes: list[dict] = []
    for f in COMPARED:
        v1, v2 = _norm(f, row1.get(f)), _norm(f, row2.get(f))
        if v1 == v2:
            agree.append(f)
        else:
            disputes.append(
                {"app": row1["name"], "field": f, "pass1": v1, "pass2": v2}
            )
    return agree, disputes


def corroborate(pass1_file: str = "pass1.validated.json",
                pass2_file: str = "pass2.raw.json") -> dict:
    p1_path = settings.data_dir / pass1_file
    p2_path = settings.data_dir / pass2_file
    for p in (p1_path, p2_path):
        if not p.exists():
            raise SystemExit(f"missing {p}")

    rows1 = {r["name"]: r for r in json.loads(p1_path.read_text(encoding="utf-8"))}
    rows2 = {r["name"]: r for r in json.loads(p2_path.read_text(encoding="utf-8"))}
    shared = [n for n in rows2 if n in rows1]

    all_disputes: list[dict] = []
    out_rows: list[dict] = []
    promoted = 0
    disputed_apps: set[str] = set()

    for name in shared:
        r1, r2 = rows1[name], rows2[name]
        agree, disputes = compare(r1, r2)

        row = dict(r2)
        row["pass_number"] = 2

        failed_either = (r1["agent_notes"].startswith("RESEARCH FAILED")
                         or r2["agent_notes"].startswith("RESEARCH FAILED"))

        if not disputes and not failed_either:
            base = min(r1["confidence"], r2["confidence"], key=LADDER.index)
            new = _up(base)
            if new != row["confidence"]:
                promoted += 1
            row["confidence"] = new
            row["agent_notes"] = (
                row.get("agent_notes", "")
                + f" [layer2: corroborated by pass 1 on all {len(agree)} compared"
                  f" fields; confidence {base} -> {new}]"
            ).strip()
        else:
            disputed_apps.add(name)
            all_disputes.extend(disputes)
            fields = ", ".join(d["field"] for d in disputes) or "run failure"
            # A disputed row is not demoted — it is simply not promoted, and it
            # is routed to Layer 3. Demoting here would double-count Layer 1.
            row["agent_notes"] = (
                row.get("agent_notes", "")
                + f" [layer2: disputed vs pass 1 on {fields}; routed to layer 3]"
            ).strip()

        out_rows.append(row)

    (settings.data_dir / "pass2.json").write_text(
        json.dumps(out_rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (settings.data_dir / "disagreements.json").write_text(
        json.dumps(all_disputes, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    field_counts: dict[str, int] = {}
    for d in all_disputes:
        field_counts[d["field"]] = field_counts.get(d["field"], 0) + 1

    # A verification layer that silently covers only part of the corpus is
    # worse than one that covers part and says which part. Name the gap.
    not_second_passed = sorted(n for n in rows1 if n not in rows2)

    summary = {
        "rows_in_pass1": len(rows1),
        "rows_second_passed": len(shared),
        "rows_not_second_passed": len(not_second_passed),
        "second_pass_coverage": (round(len(shared) / len(rows1), 4)
                                 if rows1 else None),
        "unverified_rows": not_second_passed,
        "rows_compared": len(shared),
        "fully_agreeing_rows": len(shared) - len(disputed_apps),
        "disputed_rows": len(disputed_apps),
        "confidence_promotions": promoted,
        "total_field_disagreements": len(all_disputes),
        "disagreements_by_field": dict(
            sorted(field_counts.items(), key=lambda kv: -kv[1])
        ),
        "layer3_queue": sorted(disputed_apps),
    }
    (settings.data_dir / "corroboration_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-corroborate")
    p.add_argument("--pass1", default="pass1.validated.json")
    p.add_argument("--pass2", default="pass2.raw.json")
    args = p.parse_args(argv)

    s = corroborate(args.pass1, args.pass2)
    print("=" * 66)
    print("LAYER 2 — INDEPENDENT SECOND PASS")
    print("=" * 66)
    print(f"  rows in pass 1            : {s['rows_in_pass1']}")
    print(f"  second-passed             : {s['rows_second_passed']}"
          f"  ({(s['second_pass_coverage'] or 0):.0%} coverage)")
    print(f"  NOT second-passed         : {s['rows_not_second_passed']}"
          f"  (listed in corroboration_summary.json -> unverified_rows)")
    print(f"  fully agreeing            : {s['fully_agreeing_rows']}")
    print(f"  confidence promotions     : {s['confidence_promotions']}")
    print(f"  disputed rows -> layer 3  : {s['disputed_rows']}")
    print(f"  field disagreements       : {s['total_field_disagreements']}")
    if s["disagreements_by_field"]:
        print("\n  disagreements by field")
        for f, n in s["disagreements_by_field"].items():
            print(f"    {f:<20}{n:>4}")
    if s["layer3_queue"]:
        print("\n  layer 3 queue")
        print("    " + ", ".join(s["layer3_queue"][:25]))
    print("\n  wrote data/pass2.json, data/disagreements.json,")
    print("        data/corroboration_summary.json")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
