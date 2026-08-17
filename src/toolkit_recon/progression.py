"""Progression report: convergence (measured) vs accuracy (audited).

These are two different quantities and conflating them is the single easiest
way to oversell this project, so the vocabulary is fixed:

**convergence_rate** — how often the passes agree with each other. Computed by
the chain from artifacts on disk. It measures *internal consistency only*. An
agent can converge on the same answer three times and be wrong all three
times; three identical wrong answers produce a convergence rate of 100%.

**accuracy** — how often the agent matches ground truth established by a human
reading the vendor's documentation. Comes only from the Phase 3 audit
(`toolkit_recon.audit` -> `report.py --audit` -> `human_audit.json`). Never
estimated, never inferred from confidence, never back-derived from agreement.

The output file carries both under separate top-level keys, each with its own
definition string, so a reader cannot mistake one for the other.
"""

from __future__ import annotations

import argparse
import json

from .config import settings


def _read(name: str):
    p = settings.data_dir / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _dist(rows) -> dict[str, int]:
    d = {"high": 0, "medium": 0, "low": 0}
    for r in rows or []:
        d[r["confidence"]] = d.get(r["confidence"], 0) + 1
    return d


def _apply_audit(prog: dict, audit: dict) -> None:
    """Fold the human audit into the accuracy block. Only ever copies."""
    acc = prog["accuracy"]
    acc["rows_audited"] = audit.get("rows_audited")
    acc["sample_size"] = acc["sample_size"] or audit.get("rows_in_queue")
    acc["precision_by_confidence"] = audit.get("precision_by_confidence")
    acc["precision_by_field"] = audit.get("precision_by_field")
    acc["unverifiable_count"] = audit.get("unverifiable_count")
    acc["misses"] = audit.get("misses")
    acc["precision_definition"] = audit.get("precision_definition")

    # The headline: does the confidence column separate right from wrong?
    by_conf = audit.get("precision_by_confidence") or {}
    hi = (by_conf.get("high") or {}).get("precision")
    lo = (by_conf.get("medium_low") or {}).get("precision")
    if isinstance(hi, float) and isinstance(lo, float):
        acc["confidence_signal"] = {
            "high_precision": hi,
            "medium_low_precision": lo,
            "gap": round(hi - lo, 4),
            "interpretation": (
                "confidence carries signal" if hi - lo > 0.05
                else "confidence does NOT separate correct from incorrect"
                if hi - lo <= 0 else "signal present but weak"
            ),
        }


def build(sample_size: int | None = None) -> dict:
    p1 = _read("pass1.json") or []
    p2 = _read("pass2.json")
    p3 = _read("pass3.json")
    val = _read("validation_report.json") or {}
    corr = _read("corroboration_summary.json") or {}
    l3 = _read("layer3_summary.json") or {}
    audit = _read("human_audit.json")

    res = l3.get("resolutions") or {}
    resolved_by_browser = res.get("pass1_correct", 0) + res.get("pass2_correct", 0)

    compared = corr.get("rows_compared")
    agreeing = corr.get("fully_agreeing_rows")
    conv_rate = (round(agreeing / compared, 4)
                 if isinstance(compared, int) and compared
                 and isinstance(agreeing, int) else None)

    fields_examined = l3.get("fields_examined")
    l3_rate = (round(resolved_by_browser / fields_examined, 4)
               if isinstance(fields_examined, int) and fields_examined else None)

    prog: dict = {
        "convergence": {
            "definition": (
                "Pass-to-pass agreement, computed by the chain from artifacts on "
                "disk. Measures INTERNAL CONSISTENCY ONLY. An agent that is "
                "consistently wrong scores 100% here. This is NOT accuracy."
            ),
            "pass1_to_pass2": {
                "rows_compared": compared,
                "fully_agreeing_rows": agreeing,
                "disputed_rows": corr.get("disputed_rows"),
                "convergence_rate": conv_rate,
                "field_disagreements": corr.get("total_field_disagreements"),
                "disagreements_by_field": corr.get("disagreements_by_field"),
                "confidence_promotions": corr.get("confidence_promotions"),
            },
            "layer3_browser": {
                "fields_examined": fields_examined,
                "resolved": resolved_by_browser or None,
                "resolution_rate": l3_rate,
                "unresolvable": res.get("unresolvable"),
                "both_wrong": res.get("both_wrong"),
            },
        },
        "accuracy": {
            "definition": (
                "Agreement with ground truth established by a human reading the "
                "vendor's documentation. The ONLY accuracy figure in this "
                "project. Null until the Phase 3 audit is completed; never "
                "estimated or derived from convergence."
            ),
            "source": "data/human_audit.json (via report.py --audit)",
            "sample_size": sample_size,
            "rows_audited": None,
            "precision_by_confidence": None,
            "precision_by_field": None,
            "unverifiable_count": None,
            "misses": None,
        },
        "pass_1": {
            "rows": len(p1),
            "high_conf_rows": _dist(p1)["high"],
            "flagged_by_layer1": val.get("rows_with_violations"),
            "confidence_distribution": _dist(p1),
        },
        "pass_2": {
            "rows": len(p2 or []),
            "corroborated": corr.get("fully_agreeing_rows"),
            "still_disputed": corr.get("disputed_rows"),
            "confidence_promotions": corr.get("confidence_promotions"),
            "confidence_distribution": _dist(p2),
        },
        "pass_3": {
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
        },
    }

    if isinstance(audit, dict) and audit:
        _apply_audit(prog, audit)
    return prog


def _rate(v) -> str:
    return f"{v:.1%}" if isinstance(v, float) else "PENDING"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-progression")
    p.add_argument("--sample-size", type=int, default=None)
    args = p.parse_args(argv)

    prog = build(args.sample_size)
    out = settings.data_dir / "accuracy_progression.json"
    out.write_text(json.dumps(prog, indent=2, ensure_ascii=False), encoding="utf-8")

    conv = prog["convergence"]["pass1_to_pass2"]
    l3 = prog["convergence"]["layer3_browser"]
    acc = prog["accuracy"]

    print("=" * 70)
    print("PROGRESSION")
    print("=" * 70)
    print("  CONVERGENCE — internal consistency, NOT accuracy")
    print(f"    pass1 -> pass2 : {_rate(conv['convergence_rate'])}"
          f"  ({conv['fully_agreeing_rows']}/{conv['rows_compared']} rows agree)")
    print(f"    layer 3        : {_rate(l3['resolution_rate'])}"
          f"  ({l3['resolved']}/{l3['fields_examined']} disputed fields settled)")
    print("    An agent that is consistently wrong scores 100% here.")

    print("\n  ACCURACY — human-audited ground truth")
    if acc["precision_by_confidence"]:
        for bucket in ("high", "medium_low"):
            t = acc["precision_by_confidence"].get(bucket) or {}
            print(f"    {bucket:<12} {_rate(t.get('precision'))}  n={t.get('scored')}")
        sig = acc.get("confidence_signal")
        if sig:
            print(f"    gap {sig['gap']:+.1%}: {sig['interpretation']}")
    else:
        print("    PENDING HUMAN AUDIT — not estimated, by design.")
        print("    1. python -m toolkit_recon.audit          # build the queue")
        print("    2. fill data/audit_queue.csv              # human reads the docs")
        print("    3. python -m toolkit_recon.report --audit # score it")
        print("    4. python -m toolkit_recon.progression    # fold it in here")

    print(f"\n  wrote {out}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
