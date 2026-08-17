"""LAYER 1 — structural validation over a completed pass.

Cheap, deterministic, and entirely code-side. Every rule reads either the row
itself, the execution trace, or the archived evidence on disk. No rule consults
a model, and no rule accepts a trust signal the model supplied — that would
undo the property the confidence column was built to have.

Rules fall into three actions:

  ``flag``      queue the row for an independent second pass
  ``force_low`` the row cannot be trusted at all
  ``downgrade`` the row is one level weaker than it claimed

Output is ``data/validation_report.json`` (per-rule counts + affected apps) and
``data/pass1.validated.json`` (the rows with corrections applied). pass1.json
itself is left untouched: it is the raw first pass, and the accuracy
progression has to measure it as it actually was.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from .apps import APPS
from .config import settings
from .pipeline import MCP_MENTION
from .ranking import is_official

LADDER = ["low", "medium", "high"]


def _down(conf: str) -> str:
    return LADDER[max(0, LADDER.index(conf) - 1)]


@dataclass
class Violation:
    rule: str
    slug: str
    name: str
    detail: str
    action: str


@dataclass
class RuleSpec:
    id: str
    description: str
    action: str  # flag | force_low | downgrade


RULES: list[RuleSpec] = [
    RuleSpec("R1_EVIDENCE_EMPTY", "evidence_urls must not be empty", "force_low"),
    RuleSpec("R2_EVIDENCE_NOT_FETCHED",
             "every evidence URL must be in the actually-fetched set", "flag"),
    RuleSpec("R3_MCP_UNVERIFIED",
             "has_mcp requires a fetched page that actually mentions MCP", "flag"),
    RuleSpec("R4_CONTRADICTION_TIER",
             "buildable_today=yes contradicts partner_gated/no_public_api", "flag"),
    RuleSpec("R5_CONTRADICTION_STYLE",
             "api_style=[None] contradicts buildable_today=yes", "flag"),
    RuleSpec("R6_AUTH_UNKNOWN", "auth_methods=[Unknown] cannot support a claim",
             "force_low"),
    RuleSpec("R7_NON_OFFICIAL_EVIDENCE",
             "all evidence from non-official domains", "downgrade"),
    RuleSpec("R8_THIN_EVIDENCE",
             "all archived evidence below min_doc_chars (retroactive floor)", "flag"),
]
RULE_BY_ID = {r.id: r for r in RULES}


@dataclass
class Context:
    """Everything a rule may consult. Deliberately all disk-derived."""

    slug: str
    name: str
    official_domains: tuple[str, ...]
    fetched_urls: set[str] = field(default_factory=set)
    doc_chars: dict[str, int] = field(default_factory=dict)
    doc_text: dict[str, str] = field(default_factory=dict)


def _load_contexts(pass_number: int) -> dict[str, Context]:
    """Build per-app context from the trace and the archived evidence."""
    by_name = {a.name: a for a in APPS}
    ctxs: dict[str, Context] = {}

    trace_path = settings.logs_dir / "trace.jsonl"
    traces: dict[str, dict] = {}
    if trace_path.exists():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if t.get("pass_number") == pass_number:
                traces[t["slug"]] = t  # last write wins = final attempt

    for app in APPS:
        ctx = Context(app.slug, app.name, app.official_domains)
        t = traces.get(app.slug, {})
        ctx.fetched_urls = set(t.get("urls_fetched") or [])

        # The manifest is the authority on what was actually archived, which
        # is what makes the retroactive thin-doc floor auditable.
        manifest = settings.raw_dir / app.slug / "manifest.json"
        if manifest.exists():
            try:
                m = json.loads(manifest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                m = {}
            for d in m.get("documents") or []:
                if d.get("ok") and d.get("url"):
                    ctx.doc_chars[d["url"]] = int(d.get("chars") or 0)
                    fn = d.get("file")
                    if fn:
                        p = settings.raw_dir / app.slug / fn
                        if p.exists():
                            try:
                                ctx.doc_text[d["url"]] = p.read_text(
                                    encoding="utf-8", errors="replace"
                                )
                            except OSError:
                                pass
        ctxs[app.name] = ctx

    # Apps not in APPS (shouldn't happen) still get an empty context.
    for name in by_name:
        ctxs.setdefault(name, Context(by_name[name].slug, name,
                                      by_name[name].official_domains))
    return ctxs


def check_row(row: dict, ctx: Context) -> list[Violation]:
    v: list[Violation] = []

    def add(rule_id: str, detail: str) -> None:
        v.append(Violation(rule_id, ctx.slug, ctx.name, detail,
                           RULE_BY_ID[rule_id].action))

    failed = row["agent_notes"].startswith("RESEARCH FAILED")
    urls = row.get("evidence_urls") or []

    # R1 — no evidence at all.
    if not urls:
        add("R1_EVIDENCE_EMPTY", "evidence_urls is empty")

    # R2 — cited something we never fetched. Skipped for failure rows, whose
    # placeholder `unresolved://` marker is intentional and already honest.
    if not failed and ctx.fetched_urls:
        stray = [u for u in urls if u not in ctx.fetched_urls]
        if stray:
            add("R2_EVIDENCE_NOT_FETCHED",
                f"{len(stray)} URL(s) not in fetched set: {stray[:2]}")

    # R3 — MCP claim must survive both checks, re-verified from disk.
    if row.get("has_mcp"):
        mu = row.get("mcp_evidence_url")
        if not mu:
            add("R3_MCP_UNVERIFIED", "has_mcp=true with no mcp_evidence_url")
        elif ctx.fetched_urls and mu not in ctx.fetched_urls:
            add("R3_MCP_UNVERIFIED", f"mcp_evidence_url not fetched: {mu}")
        elif mu in ctx.doc_text and not MCP_MENTION.search(ctx.doc_text[mu]):
            add("R3_MCP_UNVERIFIED", f"cited page does not mention MCP: {mu}")

    # R4 / R5 — internal contradictions.
    if row.get("buildable_today") == "yes" and row.get("access_tier") in {
        "partner_gated", "no_public_api"
    }:
        add("R4_CONTRADICTION_TIER",
            f"buildable=yes but access_tier={row['access_tier']}")

    if row.get("api_style") == ["None"] and row.get("buildable_today") == "yes":
        add("R5_CONTRADICTION_STYLE", "api_style=[None] but buildable=yes")

    # R6 — an unknown auth method cannot support any downstream claim.
    if row.get("auth_methods") == ["Unknown"] and not failed:
        add("R6_AUTH_UNKNOWN", "auth_methods=[Unknown]")

    # R7 — nothing official behind the row.
    real = [u for u in urls if u.startswith("http")]
    if real and not any(is_official(u, ctx.official_domains) for u in real):
        add("R7_NON_OFFICIAL_EVIDENCE",
            f"{len(real)} evidence URL(s), none on {ctx.official_domains}")

    # R8 — retroactive min_doc_chars floor.
    if ctx.doc_chars and all(
        c < settings.min_doc_chars for c in ctx.doc_chars.values()
    ):
        add("R8_THIN_EVIDENCE",
            f"all {len(ctx.doc_chars)} archived docs < {settings.min_doc_chars} chars "
            f"(max {max(ctx.doc_chars.values())})")

    return v


def apply_actions(row: dict, violations: list[Violation]) -> dict:
    """Return a corrected copy. Mutations are downgrades only — never upgrades."""
    out = dict(row)
    actions = {v.action for v in violations}
    if "force_low" in actions:
        out["confidence"] = "low"
    elif "downgrade" in actions:
        out["confidence"] = _down(out["confidence"])
    if violations:
        ids = ",".join(sorted({v.rule for v in violations}))
        out["agent_notes"] = (out.get("agent_notes", "") +
                              f" [layer1: {ids}]").strip()
    return out


def validate(pass_number: int = 1) -> dict:
    src = settings.data_dir / f"pass{pass_number}.json"
    if not src.exists():
        raise SystemExit(f"missing {src}; run the pipeline first")
    rows = json.loads(src.read_text(encoding="utf-8"))
    ctxs = _load_contexts(pass_number)

    all_v: list[Violation] = []
    corrected: list[dict] = []
    flagged: set[str] = set()
    thin_queue: list[str] = []

    for row in rows:
        ctx = ctxs.get(row["name"])
        if ctx is None:
            corrected.append(row)
            continue
        vs = check_row(row, ctx)
        all_v.extend(vs)
        corrected.append(apply_actions(row, vs))
        for v in vs:
            if v.action in {"flag", "force_low", "downgrade"}:
                flagged.add(ctx.slug)
            if v.rule == "R8_THIN_EVIDENCE":
                thin_queue.append(ctx.slug)

    per_rule = {
        r.id: {
            "description": r.description,
            "action": r.action,
            "count": sum(1 for v in all_v if v.rule == r.id),
            "apps": sorted({v.slug for v in all_v if v.rule == r.id}),
        }
        for r in RULES
    }

    report = {
        "pass_number": pass_number,
        "rows_checked": len(rows),
        "rows_with_violations": len({v.slug for v in all_v}),
        "total_violations": len(all_v),
        "confidence_before": _dist(rows),
        "confidence_after": _dist(corrected),
        "rules": per_rule,
        "violations": [v.__dict__ for v in all_v],
        "recheck_queue": sorted(flagged),
        "thin_evidence_rerun_queue": sorted(set(thin_queue)),
        "process_note": (
            "The min_doc_chars floor (R8) was added after this pass had already "
            "started, when a Close docs URL was seen to extract to 133 characters "
            "and still count as 'official docs reached'. Rather than discard ~50 "
            "minutes of completed work, the rule is applied retroactively here "
            "against the archived manifests, and only the apps it actually "
            "affects are queued for re-run. thin_evidence_rerun_queue is that "
            "list; it is deliberately narrow."
        ),
    }

    (settings.data_dir / "validation_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (settings.data_dir / f"pass{pass_number}.validated.json").write_text(
        json.dumps(corrected, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def _dist(rows: list[dict]) -> dict[str, int]:
    d = {"high": 0, "medium": 0, "low": 0}
    for r in rows:
        d[r["confidence"]] = d.get(r["confidence"], 0) + 1
    return d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-validate")
    p.add_argument("--pass-number", type=int, default=1)
    args = p.parse_args(argv)

    rep = validate(args.pass_number)

    print("=" * 66)
    print(f"LAYER 1 — STRUCTURAL VALIDATION (pass {args.pass_number})")
    print("=" * 66)
    print(f"  rows checked          : {rep['rows_checked']}")
    print(f"  rows with violations  : {rep['rows_with_violations']}")
    print(f"  total violations      : {rep['total_violations']}")
    print("\n  confidence before     : " + str(rep["confidence_before"]))
    print("  confidence after      : " + str(rep["confidence_after"]))
    print("\n  per rule")
    for rid, info in rep["rules"].items():
        mark = " " if info["count"] == 0 else "!"
        print(f"   {mark} {rid:<26} {info['count']:>3}  ({info['action']})")
        if info["apps"]:
            print(f"       {', '.join(info['apps'][:10])}"
                  + (" ..." if len(info["apps"]) > 10 else ""))
    print(f"\n  layer-2 recheck queue : {len(rep['recheck_queue'])} apps")
    print(f"  thin-evidence re-run  : {len(rep['thin_evidence_rerun_queue'])} apps")
    if rep["thin_evidence_rerun_queue"]:
        print("       " + ", ".join(rep["thin_evidence_rerun_queue"]))
    print("\n  wrote data/validation_report.json")
    print(f"  wrote data/pass{args.pass_number}.validated.json")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
