"""Entry point: `python -m toolkit_recon` / `toolkit-recon`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter

from .apps import APPS, BY_SLUG, AppSpec
from .config import settings
from .extract import Extractor
from .pipeline import Pipeline
from .providers import ComposioProvider, DirectProvider, build_provider
from .schema import AppResearch
from .storage import write_output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="toolkit-recon", description=__doc__)
    p.add_argument("--pass-number", type=int, default=1, choices=(1, 2, 3))
    p.add_argument("--limit", type=int, default=None, help="profile only the first N apps")
    p.add_argument("--only", default=None, help="comma-separated slugs to profile")
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument(
        "--provider", choices=("auto", "composio", "direct"), default="auto",
        help="search/fetch layer; auto uses Composio when a key is present",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="skip apps already present in this pass's checkpoint",
    )
    p.add_argument(
        "--recheck-from", type=int, default=None, metavar="N",
        help="re-profile only the weak rows of pass N (see --recheck-confidence)",
    )
    p.add_argument(
        "--recheck-confidence", default="low,medium",
        help="which confidence levels --recheck-from selects (default: low,medium)",
    )
    p.add_argument(
        "--recheck-file", default=None,
        help="file --recheck-from reads (default pass{N}.json; "
             "use pass1.validated.json to respect Layer 1 corrections)",
    )
    p.add_argument(
        "--include-flagged", action="store_true",
        help="also re-profile every app in validation_report.json's recheck queue",
    )
    p.add_argument(
        "--out", default=None, metavar="FILE",
        help="output filename under data/ (default pass{N}.json)",
    )
    p.add_argument("--fresh-trace", action="store_true", help="truncate logs/trace.jsonl first")
    return p.parse_args(argv)


def select_apps(args: argparse.Namespace) -> list[AppSpec]:
    apps = APPS
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        missing = [s for s in wanted if s not in BY_SLUG]
        if missing:
            raise SystemExit(f"unknown slug(s): {', '.join(missing)}")
        apps = [BY_SLUG[s] for s in wanted]
    if args.recheck_from:
        apps = [a for a in apps if a.name in _weak_names(args)]
    if args.limit:
        apps = apps[: args.limit]
    return apps


def _weak_names(args: argparse.Namespace) -> set[str]:
    """Names of rows a previous pass was not entitled to be confident about.

    Two sources, unioned: weak confidence, and anything Layer 1 flagged. A row
    can be structurally broken while still claiming high confidence, so the
    flag list is not a subset of the confidence list.
    """
    fname = args.recheck_file or f"pass{args.recheck_from}.json"
    path = settings.data_dir / fname
    if not path.exists():
        raise SystemExit(f"--recheck-from {args.recheck_from}: missing {path}")
    levels = {s.strip() for s in args.recheck_confidence.split(",") if s.strip()}
    prior = json.loads(path.read_text(encoding="utf-8"))
    names = {r["name"] for r in prior if r["confidence"] in levels}

    if args.include_flagged:
        rep = settings.data_dir / "validation_report.json"
        if not rep.exists():
            raise SystemExit(
                "--include-flagged needs data/validation_report.json; "
                "run `python -m toolkit_recon.validate` first"
            )
        queue = set(json.loads(rep.read_text(encoding="utf-8"))["recheck_queue"])
        names |= {a.name for a in APPS if a.slug in queue}
    return names


def print_summary(rows: list[AppResearch], elapsed: float, provider_name: str) -> None:
    conf = Counter(r.confidence for r in rows)
    tier = Counter(r.access_tier for r in rows)
    build = Counter(r.buildable_today for r in rows)
    failures = [r for r in rows if r.agent_notes.startswith("RESEARCH FAILED")]
    mcp = sum(1 for r in rows if r.has_mcp)

    bar = "=" * 62
    print(f"\n{bar}\nRUN SUMMARY\n{bar}")
    print(f"rows completed      : {len(rows)}/{len(rows)}")
    print(f"search/fetch layer  : {provider_name}")
    print(f"extraction model    : {settings.llm_model}")
    print(f"wall time           : {elapsed:.1f}s")

    print("\nconfidence distribution")
    for level in ("high", "medium", "low"):
        n = conf.get(level, 0)
        pct = (100 * n / len(rows)) if rows else 0
        print(f"  {level:<7} {n:>3}  {pct:5.1f}%  {'#' * int(pct / 2)}")

    print("\nbuildable today")
    for k in ("yes", "yes_with_caveats", "no"):
        print(f"  {k:<18} {build.get(k, 0):>3}")

    print("\naccess tier")
    for k, n in tier.most_common():
        print(f"  {k:<20} {n:>3}")

    print(f"\nofficial MCP server found : {mcp}")

    print(f"\nfailures            : {len(failures)}")
    for r in failures:
        print(f"  - {r.name}: {r.agent_notes[:110]}")

    print("\noutputs")
    print(f"  data/pass{rows[0].pass_number if rows else 1}.json")
    print("  data/raw/<slug>/")
    print("  logs/trace.jsonl")
    print(bar)


async def run(args: argparse.Namespace) -> int:
    if args.concurrency:
        settings.concurrency = args.concurrency

    if args.provider == "composio":
        if not settings.composio_api_key:
            raise SystemExit("--provider composio requires COMPOSIO_API_KEY")
        provider = ComposioProvider()
    elif args.provider == "direct":
        provider = DirectProvider()
    else:
        provider = build_provider()

    if provider.name == "direct" and args.provider == "auto":
        print(
            "! COMPOSIO_API_KEY not set - falling back to the keyless provider.\n"
            "  Set it to route search/fetch through Composio.",
            file=sys.stderr,
        )

    apps = select_apps(args)
    versions = await provider.prepare()
    if versions:
        print("composio tool versions pinned: " + json.dumps(versions))

    # The extraction framing changes with the pass: pass 2 reasons from
    # opposite priors so that agreement between passes means something.
    extractor = Extractor(pass_number=args.pass_number)
    pipe = Pipeline(provider, extractor, pass_number=args.pass_number)

    if args.fresh_trace:
        pipe.trace_log.reset()

    done_rows: dict[str, dict] = {}
    if args.resume:
        done_rows = pipe.checkpoint.load()
        before = len(apps)
        apps = [a for a in apps if a.slug not in done_rows]
        print(f"resume: {before - len(apps)} already done, {len(apps)} to go")

    total = len(apps)
    counter = {"n": 0}

    def on_done(app, row, trace) -> None:
        counter["n"] += 1
        mark = "!" if trace.status == "failed" else " "
        print(
            f"[{counter['n']:>3}/{total}]{mark} {app.name:<28} "
            f"{row.confidence:<6} {row.buildable_today:<17} {trace.wall_time_s:>5.1f}s",
            flush=True,
        )

    started = time.monotonic()
    try:
        fresh = await pipe.run(apps, on_done=on_done) if apps else {}
    finally:
        await extractor.aclose()
        if hasattr(provider, "aclose"):
            await provider.aclose()

    # Merge this run's rows over any resumed checkpoint rows, in canonical order.
    rows: list[AppResearch] = []
    for a in select_apps(args):
        if a.slug in fresh:
            rows.append(fresh[a.slug])
        elif a.slug in done_rows:
            rows.append(AppResearch.model_validate(done_rows[a.slug]))

    out = write_output(rows, args.pass_number, args.out)
    print(f"\nwrote {len(rows)} rows -> {out}")
    print_summary(rows, time.monotonic() - started, provider.name)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted - checkpoint preserved, re-run with --resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
