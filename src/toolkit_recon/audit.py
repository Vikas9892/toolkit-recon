"""PHASE 3 — human audit harness.

Ground truth is the one thing this project cannot generate for itself. The
chain's pass1 -> pass2 -> pass3 figure measures *convergence*: whether the
agent agrees with itself when asked differently. An agent can converge
beautifully on a wrong answer. Only a human reading the vendor's docs can say
whether a row is right, and this module builds the queue that human works from.

Two pieces:

* `sample()`   — stratified, seeded, reproducible selection of 20 apps.
* `write_queue()` — that sample as data/audit_queue.csv, with the agent's
  claims filled in and the verdict columns left blank.

The sample is deliberately half high-confidence. Auditing only the rows the
pipeline already doubts would confirm what we know and hide what we don't:
systematic overconfidence shows up *only* in the high-confidence rows.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from pathlib import Path

from .apps import APPS
from .config import settings

SAMPLE_SIZE = 20
HIGH_TARGET = 10
WEAK_TARGET = 10
DEFAULT_SEED = 20260817

# Gated products that stress access_tier, the hardest field in the schema.
# Matched leniently by name so they are picked up whenever they exist in the
# corpus; absence is reported rather than silently ignored.
DEFAULT_FORCED = ["Amazon SP-API", "PitchBook", "Salesforce Commerce Cloud"]

VERDICT_VOCAB = ["correct", "partially_correct", "wrong", "unverifiable"]

QUEUE_COLUMNS = [
    "slug", "name", "category", "agent_confidence",
    "agent_auth_methods", "agent_access_tier", "agent_api_style",
    "agent_api_breadth", "agent_has_mcp", "agent_buildable_today",
    "agent_primary_blocker", "evidence_urls",
    # Left blank for the auditor.
    "verdict_auth", "verdict_tier", "verdict_api", "verdict_mcp",
    "verdict_buildable", "truth_notes", "why_it_failed",
]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_rows(source: str = "auto") -> tuple[list[dict], str]:
    """Return (rows, origin). Works on a partial run.

    `auto` prefers whichever of pass1.json / the live checkpoint has more rows,
    so the queue can be built while pass 1 is still going.
    """
    final = settings.data_dir / "pass1.json"
    ckpt = settings.checkpoint_dir / "pass1.checkpoint.json"

    def _final() -> list[dict]:
        return json.loads(final.read_text(encoding="utf-8")) if final.exists() else []

    def _ckpt() -> list[dict]:
        if not ckpt.exists():
            return []
        return list(json.loads(ckpt.read_text(encoding="utf-8")).values())

    if source == "pass1":
        return _final(), "pass1.json"
    if source == "checkpoint":
        return _ckpt(), "pass1.checkpoint.json"

    a, b = _final(), _ckpt()
    if len(b) > len(a):
        return b, "pass1.checkpoint.json (partial; more rows than pass1.json)"
    return a, "pass1.json"


def _slug_for(name: str) -> str:
    for a in APPS:
        if a.name == name:
            return a.slug
    return name.lower().replace(" ", "_").replace(".", "_")


def _matches(row_name: str, wanted: str) -> bool:
    """Lenient name match, so 'Amazon SP-API' finds 'Amazon Selling Partner API'."""
    r, w = row_name.lower(), wanted.lower()
    if r == w:
        return True
    tokens = [t for t in w.replace("-", " ").split() if len(t) > 2]
    return bool(tokens) and all(t in r for t in tokens)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def sample(
    rows: list[dict],
    seed: int = DEFAULT_SEED,
    size: int = SAMPLE_SIZE,
    forced: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """Stratified, category-spread, deterministic sample.

    Constraints, in priority order:
      1. forced apps are always included when present
      2. half high-confidence, half medium/low
      3. no category over-represented (cap derived from category count)

    Returns (sampled_rows, meta). `meta` records every constraint that could
    not be satisfied — a sample that quietly fell short would undermine the
    audit it exists to support.
    """
    forced = DEFAULT_FORCED if forced is None else forced
    rng = random.Random(seed)

    by_name = {r["name"]: r for r in rows}
    categories = sorted({r["category"] for r in rows})
    n_cat = len(categories) or 1
    cap = max(2, math.ceil(size / n_cat))

    # --- forced ---
    picked: list[dict] = []
    picked_names: set[str] = set()
    forced_found: list[str] = []
    forced_missing: list[str] = []
    for want in forced:
        hit = next((r for r in rows if _matches(r["name"], want)), None)
        if hit and hit["name"] not in picked_names:
            picked.append(hit)
            picked_names.add(hit["name"])
            forced_found.append(hit["name"])
        elif not hit:
            forced_missing.append(want)

    cat_count: dict[str, int] = defaultdict(int)
    for r in picked:
        cat_count[r["category"]] += 1

    # --- strata ---
    def stratum(r: dict) -> str:
        return "high" if r["confidence"] == "high" else "weak"

    remaining = {
        "high": HIGH_TARGET - sum(1 for r in picked if stratum(r) == "high"),
        "weak": WEAK_TARGET - sum(1 for r in picked if stratum(r) == "weak"),
    }

    pools: dict[str, dict[str, list[dict]]] = {
        "high": defaultdict(list), "weak": defaultdict(list)
    }
    for r in rows:
        if r["name"] in picked_names:
            continue
        pools[stratum(r)][r["category"]].append(r)
    for s in pools:
        for c in pools[s]:
            pools[s][c].sort(key=lambda r: r["name"])
            rng.shuffle(pools[s][c])

    # Round-robin across categories so no category dominates, alternating
    # strata so the two halves fill evenly.
    order = list(categories)
    rng.shuffle(order)

    def take(s: str, allow_cap_overflow: bool) -> bool:
        for c in order:
            if remaining[s] <= 0:
                return False
            if not allow_cap_overflow and cat_count[c] >= cap:
                continue
            if pools[s][c]:
                r = pools[s][c].pop()
                picked.append(r)
                picked_names.add(r["name"])
                cat_count[c] += 1
                remaining[s] -= 1
                return True
        return False

    progressed = True
    while progressed and (remaining["high"] > 0 or remaining["weak"] > 0):
        progressed = False
        for s in ("high", "weak"):
            if remaining[s] > 0 and take(s, allow_cap_overflow=False):
                progressed = True

    # If the cap blocked us from reaching `size` (few categories, skewed data),
    # relax it rather than return a short sample — and record that we did.
    cap_relaxed = False
    progressed = True
    while progressed and len(picked) < size:
        progressed = False
        for s in ("high", "weak"):
            if remaining[s] > 0 and take(s, allow_cap_overflow=True):
                progressed = cap_relaxed = True

    # Last resort: strata targets unreachable (e.g. not enough high rows).
    # Backfill from whatever is left so the auditor still gets `size` rows.
    stratum_shortfall = {s: max(0, remaining[s]) for s in remaining}
    if len(picked) < size:
        leftovers = [r for r in rows if r["name"] not in picked_names]
        leftovers.sort(key=lambda r: r["name"])
        rng.shuffle(leftovers)
        for r in leftovers[: size - len(picked)]:
            picked.append(r)
            picked_names.add(r["name"])
            cat_count[r["category"]] += 1

    picked.sort(key=lambda r: (r["category"], r["name"]))

    got_high = sum(1 for r in picked if stratum(r) == "high")
    meta = {
        "seed": seed,
        "requested_size": size,
        "actual_size": len(picked),
        "strata": {"high": got_high, "medium_low": len(picked) - got_high},
        "strata_targets": {"high": HIGH_TARGET, "medium_low": WEAK_TARGET},
        "strata_shortfall": {k: v for k, v in stratum_shortfall.items() if v},
        "categories_in_corpus": n_cat,
        "per_category_cap": cap,
        "cap_relaxed": cap_relaxed,
        "category_distribution": dict(sorted(
            (c, sum(1 for r in picked if r["category"] == c)) for c in categories
            if any(r["category"] == c for r in picked)
        )),
        "forced_requested": forced,
        "forced_included": forced_found,
        "forced_missing": forced_missing,
        "forced_missing_note": (
            "These products are not in the 100-app corpus (src/toolkit_recon/apps.py), "
            "so no pass-1 row exists to audit. They were NOT silently replaced. "
            "To include them, add them to APPS and profile them with "
            "`python -m toolkit_recon --only <slug>` before rebuilding this queue."
        ) if forced_missing else None,
        "rows_available": len(by_name),
    }
    return picked, meta


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _fmt(v) -> str:
    if isinstance(v, list):
        return " | ".join(str(x) for x in v)
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v)


def write_queue(rows: list[dict], meta: dict, path: Path | None = None) -> Path:
    path = path or (settings.data_dir / "audit_queue.csv")
    # utf-8-sig so Excel opens it without mangling non-ASCII product names.
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=QUEUE_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "slug": _slug_for(r["name"]),
                "name": r["name"],
                "category": r["category"],
                "agent_confidence": r["confidence"],
                "agent_auth_methods": _fmt(r.get("auth_methods")),
                "agent_access_tier": _fmt(r.get("access_tier")),
                "agent_api_style": _fmt(r.get("api_style")),
                "agent_api_breadth": _fmt(r.get("api_breadth")),
                "agent_has_mcp": _fmt(r.get("has_mcp")),
                "agent_buildable_today": _fmt(r.get("buildable_today")),
                "agent_primary_blocker": _fmt(r.get("primary_blocker")),
                "evidence_urls": _fmt(r.get("evidence_urls")),
                "verdict_auth": "", "verdict_tier": "", "verdict_api": "",
                "verdict_mcp": "", "verdict_buildable": "",
                "truth_notes": "", "why_it_failed": "",
            })

    (settings.data_dir / "audit_sample_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-audit")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--size", type=int, default=SAMPLE_SIZE)
    p.add_argument("--source", choices=("auto", "pass1", "checkpoint"),
                   default="auto")
    p.add_argument("--forced", default=None,
                   help="comma-separated names to force-include")
    args = p.parse_args(argv)

    rows, origin = load_rows(args.source)
    if not rows:
        raise SystemExit("no pass-1 rows found; run the pipeline first")

    forced = ([s.strip() for s in args.forced.split(",") if s.strip()]
              if args.forced is not None else None)
    picked, meta = sample(rows, seed=args.seed, size=args.size, forced=forced)
    meta["source"] = origin
    path = write_queue(picked, meta)

    print("=" * 66)
    print("PHASE 3 — HUMAN AUDIT QUEUE")
    print("=" * 66)
    print(f"  source                : {origin} ({len(rows)} rows)")
    print(f"  seed                  : {meta['seed']}  (deterministic)")
    print(f"  sampled               : {meta['actual_size']}")
    print(f"  strata                : high={meta['strata']['high']}, "
          f"medium/low={meta['strata']['medium_low']}")
    if meta["strata_shortfall"]:
        print(f"  ! strata shortfall    : {meta['strata_shortfall']}")
    print(f"  categories covered    : {len(meta['category_distribution'])}"
          f"/{meta['categories_in_corpus']}  (cap {meta['per_category_cap']}/category)")
    if meta["cap_relaxed"]:
        print("  ! per-category cap relaxed to reach the requested size")

    print("\n  category distribution")
    for c, n in meta["category_distribution"].items():
        print(f"    {c:<26}{n:>3}")

    if meta["forced_included"]:
        print(f"\n  forced included       : {', '.join(meta['forced_included'])}")
    if meta["forced_missing"]:
        print(f"\n  ! FORCED APPS MISSING : {', '.join(meta['forced_missing'])}")
        print("    Not in the 100-app corpus, so no pass-1 row exists to audit.")
        print("    They were NOT silently substituted. See audit_sample_meta.json.")

    print(f"\n  wrote {path}")
    print(f"  wrote {settings.data_dir / 'audit_sample_meta.json'}")
    print("\n  Fill verdict_* columns with: " + " | ".join(VERDICT_VOCAB))
    print("  Then score with: python -m toolkit_recon.report --audit")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
