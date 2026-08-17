"""Accuracy progression scaffold.

The headline verification finding is X < Y < Z — accuracy rising across the
three layers. Those three numbers are the one thing in this project that
cannot be computed from the pipeline's own output, because a pipeline scoring
its own correctness is not measuring accuracy, it is measuring self-consistency.

So this module does two separate things and keeps them strictly apart:

* It **computes** everything that is a matter of record: how many rows each
  layer touched, how many it flagged, resolved, or left disputed. These come
  from the artifacts on disk.
* It **reserves** `correct` and `accuracy` as nulls, to be filled from the
  Phase 3 human audit of a labelled sample. They are never estimated, inferred
  from confidence, or back-derived from agreement rates.

`--audit-file` ingests the human labels once they exist and fills in the rest.
Until then the file ships with nulls and an explicit note saying why.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import settings

AUDIT_TEMPLATE_NAME = "human_audit_template.json"

# Fields the human audit adjudicates. Kept identical to what Layer 2 compares,
# so "correct" means the same thing at every stage.
AUDITED_FIELDS = [
    "auth_methods", "access_tier", "api_style",
    "api_breadth", "has_mcp", "buildable_today",
]


def _read(name: str) -> list | dict | None:
    p = settings.data_dir / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _dist(rows: list[dict] | None) -> dict[str, int]:
    d = {"high": 0, "medium": 0, "low": 0}
    for r in rows or []:
        d[r["confidence"]] = d.get(r["confidence"], 0) + 1
    return d


def build(sample_size: int | None = None) -> dict:
    p1 = _read("pass1.json") or []
    p1v = _read("pass1.validated.json")
    p2 = _read("pass2.json")
    p3 = _read("pass3.json")
    val = _read("validation_report.json") or {}
    corr = _read("corroboration_summary.json") or {}
    l3 = _read("layer3_summary.json") or {}
    audit = _read("human_audit.json")

    res = (l3.get("resolutions") or {})
    resolved_by_browser = res.get("pass1_correct", 0) + res.get("pass2_correct", 0)

    prog: dict = {
        "sample_size": sample_size if sample_size is not None else (
            len(audit) if isinstance(audit, list) else None
        ),
        "ground_truth_source": "human audit of a labelled sample (Phase 3)",
        "pass_1": {
            "correct": None,
            "accuracy": None,
            "rows": len(p1),
            "high_conf_rows": _dist(p1)["high"],
            "flagged": val.get("rows_with_violations"),
            "confidence_distribution": _dist(p1),
        },
        "pass_2": {
            "correct": None,
            "accuracy": None,
            "rows": len(p2 or []),
            "resolved": corr.get("fully_agreeing_rows"),
            "still_disputed": corr.get("disputed_rows"),
            "confidence_promotions": corr.get("confidence_promotions"),
            "confidence_distribution": _dist(p2),
        },
        "pass_3": {
            "correct": None,
            "accuracy": None,
            "rows": len(p3 or []),
            "resolved_by_browser": resolved_by_browser or None,
            "unresolvable": res.get("unresolvable"),
            "both_wrong": res.get("both_wrong"),
            "confidence_distribution": _dist(p3),
        },
        "structural_effects": {
            "layer1_confidence_before": val.get("confidence_before"),
            "layer1_confidence_after": val.get("confidence_after"),
            "layer1_violations": val.get("total_violations"),
            "layer2_field_disagreements": corr.get("total_field_disagreements"),
            "layer2_disagreements_by_field": corr.get("disagreements_by_field"),
            "layer3_fields_examined": l3.get("fields_examined"),
        },
        "note": (
            "correct/accuracy are intentionally null. They are the only figures "
            "here that require ground truth, and the pipeline cannot supply its "
            "own. Populate them with `python -m toolkit_recon.progression "
            "--audit-file data/human_audit.json` after a human labels the "
            "sample. Every other number on this page is a matter of record, "
            "computed from artifacts on disk. Agreement rates and confidence "
            "distributions are deliberately NOT used as proxies for accuracy: "
            "two passes can agree and both be wrong, which is exactly what the "
            "both_wrong resolution exists to catch."
        ),
    }

    if isinstance(audit, list) and audit:
        _apply_audit(prog, audit, p1v or p1, p2, p3)
    return prog


def _score(rows: list[dict] | None, labels: list[dict]) -> tuple[int, int]:
    """Count rows whose audited fields all match the human label."""
    if not rows:
        return 0, 0
    by_name = {r["name"]: r for r in rows}
    correct = considered = 0
    for lab in labels:
        row = by_name.get(lab["name"])
        if row is None:
            continue  # this pass did not cover the row; not scored against it
        considered += 1
        ok = True
        for f in AUDITED_FIELDS:
            if f not in lab:
                continue
            a, b = row.get(f), lab[f]
            if isinstance(a, list) or isinstance(b, list):
                a, b = sorted(a or []), sorted(b or [])
            if a != b:
                ok = False
                break
        correct += ok
    return correct, considered


def _apply_audit(prog: dict, labels: list[dict], p1, p2, p3) -> None:
    prog["sample_size"] = len(labels)
    for key, rows in (("pass_1", p1), ("pass_2", p2), ("pass_3", p3)):
        correct, considered = _score(rows, labels)
        prog[key]["correct"] = correct
        prog[key]["scored_against"] = considered
        prog[key]["accuracy"] = round(correct / considered, 4) if considered else None
    prog["note"] = (
        "Populated from data/human_audit.json. `accuracy` is per-row exact match "
        "across all audited fields, scored only over rows that pass covered — "
        "passes 2 and 3 are deliberately narrower than pass 1, so their "
        "denominators differ and are reported as scored_against."
    )


def write_template(limit: int = 20) -> Path:
    """Emit a labelling sheet for the human auditor.

    Sampled to be useful rather than flattering: disputed rows first, then
    low/medium confidence, then a few high-confidence rows as a control — if
    the high rows are not spot-checked, a systematic overconfidence bug stays
    invisible.
    """
    p1 = _read("pass1.validated.json") or _read("pass1.json") or []
    disputes = _read("disagreements.json") or []
    disputed = {d["app"] for d in disputes}

    order = {"low": 0, "medium": 1, "high": 2}
    ranked = sorted(
        p1,
        key=lambda r: (0 if r["name"] in disputed else 1, order[r["confidence"]]),
    )
    picked = ranked[: max(1, limit)]

    template = [
        {
            "name": r["name"],
            "_pipeline_said": {f: r.get(f) for f in AUDITED_FIELDS},
            "_confidence": r["confidence"],
            "_disputed": r["name"] in disputed,
            "_evidence_urls": r.get("evidence_urls", [])[:3],
            **{f: None for f in AUDITED_FIELDS},
            "_auditor_notes": "",
        }
        for r in picked
    ]
    path = settings.data_dir / AUDIT_TEMPLATE_NAME
    path.write_text(json.dumps(template, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-progression")
    p.add_argument("--sample-size", type=int, default=None)
    p.add_argument("--audit-file", default=None,
                   help="human labels; copied to data/human_audit.json and scored")
    p.add_argument("--write-template", type=int, metavar="N", default=None,
                   help="emit a labelling sheet for N rows and exit")
    args = p.parse_args(argv)

    if args.write_template:
        path = write_template(args.write_template)
        print(f"wrote {path}")
        print("Fill in the null fields with ground truth, save as "
              "data/human_audit.json, then re-run with --audit-file.")
        return 0

    if args.audit_file:
        src = Path(args.audit_file)
        if not src.exists():
            raise SystemExit(f"missing {src}")
        (settings.data_dir / "human_audit.json").write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8")

    prog = build(args.sample_size)
    out = settings.data_dir / "accuracy_progression.json"
    out.write_text(json.dumps(prog, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=" * 66)
    print("ACCURACY PROGRESSION")
    print("=" * 66)
    for k in ("pass_1", "pass_2", "pass_3"):
        s = prog[k]
        acc = s["accuracy"]
        acc_s = f"{acc:.1%}" if isinstance(acc, float) else "PENDING HUMAN AUDIT"
        print(f"  {k}: rows={s['rows']:<4} correct={s['correct']}  accuracy={acc_s}")
    if prog["pass_1"]["accuracy"] is None:
        print("\n  X < Y < Z is not reported because ground truth does not exist yet.")
        print("  Run: python -m toolkit_recon.progression --write-template 20")
    print(f"\n  wrote {out}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
