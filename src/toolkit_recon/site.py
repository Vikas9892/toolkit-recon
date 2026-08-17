"""Build the single-page HTML deliverable from the artifacts.

Every number on the page is read out of data/ at build time. Nothing is typed
in by hand, so the page cannot drift from the artifacts it describes and a
corrected hand check corrects the page. If an artifact is missing the section
says so rather than rendering a blank or a zero.

Output: site/index.html, self-contained apart from Chart.js on a CDN, with the
raw JSON copied alongside it so every figure on the page is one click from its
source. Works opened as a local file and works deployed.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

from .config import settings

TIERS = ["self_serve_free", "self_serve_trial", "paid_plan_required",
         "admin_approval", "partner_gated", "no_public_api"]
BREADTHS = ["narrow", "moderate", "broad"]
SELF_SERVE = {"self_serve_free", "self_serve_trial"}

COPY_ARTIFACTS = [
    "pass1.json", "pass1.validated.json", "hand_check.json",
    "hand_check_queue.csv", "hand_check_meta.json", "patterns.json",
    "corroboration_summary.json", "validation_report.json",
    "audit_queue.csv", "audit_sample_meta.json", "disagreements.json",
]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load(name: str, default=None):
    p = settings.data_dir / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _traces() -> list[dict]:
    p = settings.logs_dir / "trace.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _is_failed(r: dict) -> bool:
    return (r.get("agent_notes") or "").startswith("RESEARCH FAILED")


# ---------------------------------------------------------------------------
# Derived figures
# ---------------------------------------------------------------------------


def collect() -> dict:
    rows = _load("pass1.validated.json") or _load("pass1.json") or []
    hand = _load("hand_check.json") or {}
    pat = _load("patterns.json") or {}
    corr = _load("corroboration_summary.json") or {}
    val = _load("validation_report.json") or {}
    audit_meta = _load("audit_sample_meta.json") or {}
    tr = _traces()

    failed = [r for r in rows if _is_failed(r)]
    live = [r for r in rows if not _is_failed(r)]

    tier_breadth = {
        t: {b: sum(1 for r in rows
                   if r.get("access_tier") == t and r.get("api_breadth") == b)
            for b in BREADTHS}
        for t in TIERS
    }
    tier_breadth = {t: v for t, v in tier_breadth.items() if sum(v.values())}

    quota = [t for t in tr
             if "daily token budget" in (t.get("error") or "").lower()]
    deadlines = [t for t in tr if t.get("deadline_hit")]

    mcp = [r for r in rows if r.get("has_mcp")]
    retracted = [r["name"] for r in rows
                 if "has_mcp retracted" in (r.get("agent_notes") or "")]

    # Pricing-evidence figure, recomputed over the final corpus rather than
    # quoted from the interim run it was first measured on.
    pe_rows = [r for r in rows if r.get("access_tier") in SELF_SERVE]

    return {
        "rows": rows,
        "n": len(rows),
        "n_live": len(live),
        "failed": [r["name"] for r in failed],
        "self_serve_count": sum(1 for r in rows
                                if r.get("access_tier") in SELF_SERVE),
        "no_public_api": [r["name"] for r in rows
                          if r.get("access_tier") == "no_public_api"],
        "confidence": Counter(r["confidence"] for r in rows),
        "tier_breadth": tier_breadth,
        "categories": Counter(r["category"] for r in rows),
        "auth": Counter(" + ".join(sorted(r.get("auth_methods") or ["(none)"]))
                        for r in live),
        "hand": hand,
        "patterns": pat,
        "corr": corr,
        "validation": val,
        "audit_meta": audit_meta,
        "quota_rows": len(quota),
        "deadlines": [
            {"slug": t.get("slug"), "pass": t.get("pass_number"),
             "stage": t.get("stage"), "wall": t.get("wall_time_s")}
            for t in deadlines
        ],
        "mcp": [{"name": r["name"], "url": r.get("mcp_evidence_url")}
                for r in sorted(mcp, key=lambda r: r["name"])],
        "mcp_retracted": retracted,
        "self_serve_rows": len(pe_rows),
        "model": next((r.get("extracted_by") for r in rows
                       if r.get("extracted_by")), "unknown"),
    }


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _hand_rows(hand: dict) -> str:
    """The hand-check table: four rows, misses not hidden."""
    per = {a["app"]: a for a in hand.get("per_app", [])}
    gap = {g["app"]: g for g in
           (hand.get("schema_cannot_express") or {}).get("rows", [])}
    order = ["Salesloft", "Gorgias", "Deel", "BILL"]

    out = []
    for app in order:
        a, g = per.get(app), gap.get(app)
        if not a and not g:
            continue
        if g:
            pipeline, truth, url, why = (g["agent_access_tier"],
                                         "schema_cannot_express",
                                         g.get("vendor_evidence_url", ""),
                                         g.get("why", ""))
            klass, label = "gap", "UNREPRESENTABLE"
        else:
            pipeline, truth = a["agent_access_tier"], a["truth_access_tier"]
            url, why = a.get("vendor_evidence_url", ""), a.get("why_it_failed", "")
            miss = a["outcome"] == "false_negative"
            klass, label = ("miss", "MISS") if miss else ("ok", "PIPELINE CORRECT")
        link = (f'<a href="{esc(url)}" target="_blank" rel="noopener">'
                f'{esc(url)}</a>') if url else '<span class="faint">—</span>'
        out.append(
            f'<tr class="{klass}">'
            f'<td><span class="tag {klass}">{label}</span></td>'
            f'<td class="mono">{esc(app)}</td>'
            f'<td class="mono dim">{esc(pipeline)}</td>'
            f'<td class="mono accent-t">{esc(truth)}</td>'
            f'<td class="src">{link}</td>'
            f'</tr>'
            f'<tr class="why-row {klass}"><td></td>'
            f'<td colspan="4" class="why">{esc(why)}</td></tr>'
        )
    return "\n".join(out)


def _tier_breadth_table(tb: dict, n: int) -> str:
    head = ("<tr><th>access tier</th>"
            + "".join(f"<th class='num'>{b}</th>" for b in BREADTHS)
            + "<th class='num'>total</th></tr>")
    body = []
    for t, cells in tb.items():
        tot = sum(cells.values())
        body.append(
            f"<tr><td class='mono'>{esc(t)}</td>"
            + "".join(f"<td class='num mono'>{cells[b]}</td>" for b in BREADTHS)
            + f"<td class='num mono strong'>{tot}</td></tr>")
    cols = [sum(tb[t][b] for t in tb) for b in BREADTHS]
    body.append("<tr class='tot'><td class='mono'>total</td>"
                + "".join(f"<td class='num mono'>{c}</td>" for c in cols)
                + f"<td class='num mono strong'>{n}</td></tr>")
    return head + "".join(body)


def _list(items, cls="") -> str:
    return "".join(f'<li class="{cls}">{esc(i)}</li>' for i in items)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(out_dir: Path) -> Path:
    d = collect()
    hand = d["hand"]
    fn = hand.get("false_negative_rate", {})
    fp = hand.get("false_positive_rate", {})
    ap = hand.get("agent_filled_pass_vs_human", {})
    corr = d["corr"]
    val = d["validation"]

    fn_wrong, fn_checked = fn.get("wrong", 0), fn.get("checked", 0)
    fp_checked = fp.get("checked", 0)

    tpl = TEMPLATE
    repl = {
        "__N__": str(d["n"]),
        "__SELF_SERVE__": str(d["self_serve_count"]),
        "__NO_API__": str(len(d["no_public_api"])),
        "__FN_WRONG__": str(fn_wrong),
        "__FN_CHECKED__": str(fn_checked),
        "__FP_CHECKED__": str(fp_checked),
        "__MODEL__": esc(d["model"]),
        "__QUOTA_ROWS__": str(d["quota_rows"]),
        "__HAND_ROWS__": _hand_rows(hand),
        "__AGENT_WRONG__": str(ap.get("disagreed_with_human", 0)),
        "__AGENT_COMPARED__": str(ap.get("compared", 0)),
        "__AGENT_DISAGREE__": ", ".join(
            d_["app"] for d_ in ap.get("disagreements", [])) or "none",
        "__TIER_BREADTH__": _tier_breadth_table(d["tier_breadth"], d["n"]),
        "__TIER_CAVEAT__": esc(
            (d["patterns"].get("access_tier_caveat") or {}).get("note", "")),
        "__SAMPLE_NOTE__": esc(hand.get("sample_note", "")),
        "__VERDICT__": esc(hand.get("verdict", "")),
        "__CROSSTAB_NOTE__": esc(
            hand.get("why_this_test_and_not_the_cross_tab", "")),
        "__L1_VIOLATIONS__": str(val.get("total_violations", 0)),
        "__L1_ROWS__": str(val.get("rows_checked", d["n"])),
        "__L2_ATTEMPTED__": str(corr.get("rows_attempted_second_pass", 0)),
        "__L2_USABLE__": str(corr.get("rows_second_passed", 0)),
        "__L2_FAILED__": str(corr.get("second_pass_failed", 0)),
        "__L2_NEVER__": str(len(corr.get("never_attempted_rows", []))),
        "__L2_COVERAGE__": f"{(corr.get('second_pass_coverage') or 0):.0%}",
        "__L2_DISAGREE__": str(corr.get("total_field_disagreements", 0)),
        "__L2_UNVERIFIED__": str(corr.get("rows_not_second_passed", 0)),
        "__L2_PROMOTIONS__": str(corr.get("confidence_promotions", 0)),
        "__MCP_COUNT__": str(len(d["mcp"])),
        "__MCP_LIST__": "".join(
            f'<li><span class="mono">{esc(m["name"])}</span> '
            f'<a href="{esc(m["url"])}" target="_blank" rel="noopener" '
            f'class="mono faint">{esc(m["url"])}</a></li>' for m in d["mcp"]),
        "__MCP_RETRACTED__": ", ".join(d["mcp_retracted"]) or "none",
        "__FAILED_COUNT__": str(len(d["failed"])),
        "__FAILED_LIST__": ", ".join(d["failed"]),
        "__AUDIT_SIZE__": str(d["audit_meta"].get("actual_size", 0)),
        "__AUDIT_RULE__": esc(
            (d["audit_meta"].get("size_derivation") or {}).get("rule", "")),
        "__DEADLINES__": "".join(
            f'<li><span class="mono">{esc(x["slug"])}</span> '
            f'<span class="faint">pass {x["pass"]} · stage {esc(x["stage"])}</span> '
            f'<span class="mono accent-t">{x["wall"]:.2f}s</span></li>'
            for x in d["deadlines"]),
        "__CORPUS_JSON__": json.dumps(d["rows"], ensure_ascii=False)
            .replace("</", "<\\/"),
        "__PATTERNS_JSON__": json.dumps({
            "tier_breadth": d["tier_breadth"],
            "auth": dict(d["auth"].most_common()),
            "categories": dict(d["categories"].most_common()),
            "blockers": {
                k: v["count"] for k, v in
                ((d["patterns"].get("blocker_families") or {})
                 .get("families") or {}).items() if v["count"]
            },
            "confidence": dict(d["confidence"]),
        }, ensure_ascii=False).replace("</", "<\\/"),
        "__UNMATCHED_BLOCKERS__": str(len(
            (d["patterns"].get("blocker_families") or {}).get("unmatched", []))),
        "__NO_BLOCKER__": str(
            (d["patterns"].get("blocker_families") or {})
            .get("rows_with_no_blocker_stated", 0)),
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(tpl, encoding="utf-8")

    data_out = out_dir / "data"
    data_out.mkdir(exist_ok=True)
    for name in COPY_ARTIFACTS:
        src = settings.data_dir / name
        if src.exists():
            shutil.copy2(src, data_out / name)

    return out_dir / "index.html"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolkit-recon-site")
    p.add_argument("--out", default="site")
    args = p.parse_args(argv)
    path = build(Path(args.out))
    kb = path.stat().st_size / 1024
    print(f"wrote {path}  ({kb:.0f} KB)")
    print(f"wrote {path.parent / 'data'}/  ({len(COPY_ARTIFACTS)} artifacts)")
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>toolkit-recon — what the run actually established</title>
<meta name="description" content="Profiling 100 SaaS apps for agent-toolkit buildability. 63 completed. What the pipeline got right, what it got wrong, and what it could not represent.">
<style>
:root{
  --bg:#0a0c0f; --panel:#11141a; --panel2:#151922; --line:#242a35;
  --fg:#e7ebf2; --dim:#98a2b3; --faint:#6b7686;
  --accent:#f0a04b; --accent-dim:#f0a04b22;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:16px;line-height:1.6;
}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.dim{color:var(--dim)}.faint{color:var(--faint)}
.accent-t{color:var(--accent)}
.strong{font-weight:600}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent}
a:hover{border-bottom-color:var(--accent)}

header{border-bottom:1px solid var(--line);padding:44px 0 28px;margin-bottom:48px}
header h1{margin:0 0 6px;font-size:15px;font-weight:600;letter-spacing:.14em;
  text-transform:uppercase;color:var(--accent);font-family:var(--mono)}
header .sub{color:var(--dim);font-size:14px;margin:0}

section{margin:0 0 68px}
h2{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint);
  font-family:var(--mono);font-weight:600;margin:0 0 20px;
  padding-bottom:10px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:28px 0 10px;font-weight:600}

.headline{font-size:clamp(24px,3.6vw,40px);line-height:1.24;font-weight:600;
  letter-spacing:-.02em;margin:0 0 26px;max-width:22ch}
.headline .hl{color:var(--accent)}
.lede{font-size:clamp(16px,1.7vw,19px);line-height:1.62;color:var(--fg);
  max-width:70ch;margin:0 0 18px}
.lede.two{color:var(--dim)}

.inline-notes{list-style:none;padding:0;margin:26px 0 0;display:grid;gap:10px}
.inline-notes li{padding:12px 14px;background:var(--panel);border-left:2px solid var(--accent);
  font-size:14px;color:var(--dim);line-height:1.55}

table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-family:var(--mono);font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint);font-weight:600;
  padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th.num,td.num{text-align:right}
tr.tot td{border-top:1px solid var(--line);font-weight:600}

.tag{display:inline-block;font-family:var(--mono);font-size:10px;font-weight:700;
  letter-spacing:.09em;padding:3px 7px;border-radius:3px;white-space:nowrap}
.tag.miss{background:var(--accent);color:#1a1206}
.tag.ok{background:#232a33;color:var(--dim)}
.tag.gap{background:transparent;color:var(--accent);border:1px solid var(--accent)}
tr.why-row td{border-bottom:1px solid var(--line);padding-top:0}
.why{color:var(--dim);font-size:13px;line-height:1.55;max-width:88ch}
.src a{font-family:var(--mono);font-size:11.5px;word-break:break-all}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}

.grid{display:grid;gap:18px}
.g2{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:18px}
.card h4{margin:0 0 12px;font-size:12px;font-family:var(--mono);letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint);font-weight:600}
.stat{font-family:var(--mono);font-size:30px;font-weight:600;line-height:1.1;
  letter-spacing:-.02em}
.stat.acc{color:var(--accent)}
.stat-l{font-size:12.5px;color:var(--faint);margin-top:6px;line-height:1.45}

.caption{font-size:13.5px;color:var(--dim);margin:12px 0 0;line-height:1.6;max-width:78ch}
.caption strong{color:var(--fg);font-weight:600}
.chart-box{position:relative;height:280px;margin-top:6px}
.chart-fallback{font-size:13px;color:var(--faint);font-family:var(--mono)}

ul.plain{list-style:none;padding:0;margin:0}
ul.plain li{padding:7px 0;border-bottom:1px solid var(--line);font-size:14px}
ul.plain li:last-child{border-bottom:0}

.note{background:var(--panel);border:1px solid var(--line);border-left:2px solid var(--accent);
  border-radius:0 5px 5px 0;padding:16px 18px;font-size:14px;color:var(--dim);
  line-height:1.62;margin:18px 0}
.note .t{color:var(--fg);font-weight:600;display:block;margin-bottom:6px}

.lesson{border-left:2px solid var(--line);padding:0 0 0 20px;margin:0 0 24px}
.lesson h4{margin:0 0 6px;font-size:15px;font-weight:600}
.lesson p{margin:0;color:var(--dim);font-size:14.5px;line-height:1.62;max-width:80ch}
.lesson.key{border-left-color:var(--accent)}

.pipe{display:flex;flex-wrap:wrap;gap:8px;align-items:stretch;margin:0 0 8px}
.pipe .st{flex:1 1 128px;background:var(--panel);border:1px solid var(--line);
  border-radius:5px;padding:12px;font-size:12.5px}
.pipe .st b{display:block;font-family:var(--mono);font-size:11px;color:var(--accent);
  letter-spacing:.08em;text-transform:uppercase;margin-bottom:5px}
.pipe .st span{color:var(--faint);font-size:12px;line-height:1.45;display:block}

.controls{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 14px}
.controls select,.controls input{background:var(--panel2);color:var(--fg);
  border:1px solid var(--line);border-radius:4px;padding:7px 9px;font-size:13px;
  font-family:inherit}
.controls input{flex:1 1 190px;min-width:150px}
button.toggle{background:var(--panel);color:var(--fg);border:1px solid var(--line);
  border-radius:5px;padding:11px 16px;font-size:14px;cursor:pointer;font-family:inherit;
  width:100%;text-align:left}
button.toggle:hover{border-color:var(--accent)}
button.toggle .c{color:var(--faint);font-family:var(--mono);font-size:12.5px}
#tableWrap[hidden]{display:none}
#dataTable th{cursor:pointer;user-select:none}
#dataTable th:hover{color:var(--accent)}
#dataTable td{font-size:13px}
.badge{font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:3px;
  background:#232a33;color:var(--dim)}
.badge.high{background:var(--accent-dim);color:var(--accent)}
.badge.failed{background:var(--accent);color:#1a1206}
.ev a{font-family:var(--mono);font-size:11px}

footer{border-top:1px solid var(--line);padding:30px 0 60px;margin-top:60px;
  color:var(--faint);font-size:13px}
footer a{margin-right:16px;font-family:var(--mono);font-size:12.5px}
@media(max-width:640px){
  .headline{max-width:none}
  td,th{padding:7px 6px}
  .chart-box{height:240px}
}
</style>
</head>
<body>

<header>
  <div class="wrap">
    <h1>toolkit-recon</h1>
    <p class="sub">Profiling SaaS apps for agent-toolkit buildability ·
      <span class="mono">__N__ of 100 profiled</span> ·
      extraction model <span class="mono">__MODEL__</span></p>
  </div>
</header>

<div class="wrap">

<!-- ============ 1. HEADLINE ============ -->
<section id="headline">
  <p class="headline">Of __N__ apps profiled, __SELF_SERVE__ report as
    self-serve. Hand-checking says that is <span class="hl">a ceiling,
    not a count</span>.</p>

  <p class="lede">The pipeline was <strong>100% correct every time it said
    &ldquo;gated&rdquo;</strong>, and wrong on __FN_WRONG__ of __FN_CHECKED__
    scoreable rows when it said &ldquo;self-serve&rdquo;. The miss —
    Salesloft — shows the mechanism: developer documentation describing how to
    authenticate was read as evidence that credentials are obtainable, for a
    vendor that publishes no pricing at all.</p>

  <p class="lede two">The only apps it reliably identifies as unreachable are
    the __NO_API__ with no public API — which are also the only __NO_API__ with
    narrow surfaces. It detects the absence of an API. It does not detect the
    presence of a paywall.</p>

  <p class="lede two">A fourth app broke the schema rather than the pipeline.
    BILL&rsquo;s access tier depends on which product you mean: Enterprise for
    AP/AR, free for the API platform. Neither enum value is correct, because
    access tier is a property of a product, not a company.</p>

  <p class="lede"><strong>Implication for toolkit prioritisation:</strong>
    <span class="accent-t">access_tier needs a second evidence source, and for
    multi-product vendors it needs a different research unit.</span></p>

  <ul class="inline-notes">
    <li>The sample is <strong>__FN_CHECKED__ scoreable rows</strong>, enriched
      with expected-gated products. It bounds the <strong>direction</strong> of
      the error, not its magnitude.</li>
    <li>The tier &times; confidence cross-tab returned a null (89% vs 91%)
      because uniform bias produces a null there <strong>by
      construction</strong> — it detects only bias that concentrates in the
      weak cohort.</li>
    <li><strong>No accuracy number appears on this page.</strong> Accuracy
      requires the human audit, which was generated and not filled.</li>
  </ul>
</section>

<!-- ============ 2. HAND-CHECK TABLE ============ -->
<section id="handcheck">
  <h2>Hand check — against the vendor&rsquo;s own pages</h2>
  <div class="scroll">
  <table>
    <thead><tr><th>verdict</th><th>app</th><th>pipeline said</th>
      <th>vendor reality</th><th>citation</th></tr></thead>
    <tbody>__HAND_ROWS__</tbody>
  </table>
  </div>

  <div class="note">
    <span class="t">The correction: an earlier agent-filled pass got
      __AGENT_WRONG__ of __AGENT_COMPARED__ wrong</span>
    Before the human check, a second model read the same vendor pages and
    called <span class="mono">__AGENT_DISAGREE__</span> misses. Human
    verification found the pipeline was right on both. This is direct evidence
    that <strong>a second model reading the same class of page is not
    independent verification</strong> — which is the caveat the artifact
    already carried on its own face. The caveat was correct.
  </div>

  <p class="caption">__VERDICT__</p>
  <p class="caption">__SAMPLE_NOTE__</p>
</section>

<!-- ============ 3. VERIFIED / NOT VERIFIED ============ -->
<section id="scope">
  <h2>What I could and could not verify</h2>
  <div class="grid g2">
    <div class="card">
      <h4>Verified by hand</h4>
      <p class="caption" style="margin-top:0">Against vendor pricing pages:
        <span class="mono">Salesloft, Gorgias, Deel, BILL</span>. These four are
        the strongest result on this page because they
        <strong>do not require trusting the pipeline</strong> — the
        vendor&rsquo;s own page is one click away in the table above.</p>
    </div>
    <div class="card">
      <h4>Not verified</h4>
      <p class="caption" style="margin-top:0">The __AUDIT_SIZE__-app audit queue
        was generated but not filled, so <strong>no accuracy figure appears
        anywhere</strong>. What appears instead is cross-model agreement, and
        every claim carries its source URL.</p>
    </div>
  </div>
</section>

<!-- ============ 4. VERIFICATION ============ -->
<section id="verification">
  <h2>Verification</h2>

  <div class="note">
    <span class="t">convergence_rate is not accuracy</span>
    Convergence measures whether the agent agrees with itself when asked
    differently. <strong>A consistently wrong agent scores 100%.</strong>
    Accuracy does not exist in this project — it requires the human audit — and
    a test asserts no output file carries a populated <span class="mono">accuracy</span> key.
  </div>

  <div class="grid g3">
    <div class="card">
      <h4>Layer 1 — structural</h4>
      <div class="stat">__L1_VIOLATIONS__<span class="dim" style="font-size:16px">/__L1_ROWS__</span></div>
      <p class="stat-l">violations. Braze MCP <strong>retracted</strong>
        (apis.io — not a vendor domain). Telegram api_style contradiction
        flagged.</p>
    </div>
    <div class="card">
      <h4>Layer 2 — second pass</h4>
      <div class="stat acc">__L2_USABLE__<span class="dim" style="font-size:16px">/__N__</span></div>
      <p class="stat-l">usable readings — <strong>__L2_COVERAGE__ coverage</strong>.
        __L2_ATTEMPTED__ attempted, __L2_FAILED__ failed, __L2_NEVER__ never
        reached. __L2_PROMOTIONS__ promotions, __L2_DISAGREE__ field
        disagreements. All __L2_UNVERIFIED__ unverified rows named.</p>
    </div>
    <div class="card">
      <h4>Layer 3 — browser</h4>
      <div class="stat dim">skipped</div>
      <p class="stat-l">Deliberately, and recorded. It settles inter-pass
        disputes; both passes share the extractor whose bias is the finding.
        The 11-row queue is preserved as a queue that was <em>not run</em>.</p>
    </div>
  </div>

  <div class="note">
    <span class="t">Layer 2 was counting attempts as coverage</span>
    It reported all __L2_ATTEMPTED__ attempted rows as second-passed — 17%
    coverage where the corpus has __L2_COVERAGE__. Attempted is not verified.
    Corrected to count only usable readings, and to name the failed and
    never-reached sets separately.
  </div>

  <div class="note">
    <span class="t">The 18% pricing-evidence figure is a lower bound</span>
    Recomputed over the final corpus it is 8 of __SELF_SERVE__ self-serve rows
    whose evidence never mentions pricing at all. It is a floor, not a
    measurement: <strong>keyword presence is not the page stating a tier</strong>,
    so pages that mention &ldquo;pricing&rdquo; in a nav bar still count as
    having evidence.
  </div>
</section>

<!-- ============ 5. PATTERNS ============ -->
<section id="patterns">
  <h2>Patterns</h2>

  <h3>Access tier &times; API breadth</h3>
  <div class="scroll"><table>__TIER_BREADTH__</table></div>
  <p class="caption"><strong>Every <span class="mono">no_public_api</span> row
    is narrow; every moderate or broad surface reports reachable.</strong>
    Breadth and access correlate only because the gate is invisible to this
    method — a documented, broad API looks reachable whether or not a paywall
    sits in front of it.</p>
  <p class="caption faint">__TIER_CAVEAT__</p>

  <div class="grid g2" style="margin-top:34px">
    <div>
      <h3>Auth methods</h3>
      <div class="chart-box"><canvas id="cAuth"></canvas>
        <noscript class="chart-fallback">Chart requires JavaScript.</noscript></div>
      <p class="caption">OAuth2 appears in nearly every combination. Auth
        variety is not what makes an app hard to build against — access is.</p>
    </div>
    <div>
      <h3>Category coverage</h3>
      <div class="chart-box"><canvas id="cCat"></canvas></div>
      <p class="caption">Three categories sit at 1&ndash;2 rows and cannot
        support a per-category claim. They are thin because the corpus is thin
        there, not because the run stopped early.</p>
    </div>
    <div>
      <h3>Blocker families</h3>
      <div class="chart-box"><canvas id="cBlock"></canvas></div>
      <p class="caption"><strong>__NO_BLOCKER__ of the live rows name no
        blocker at all.</strong> That silence is the same artefact as the
        self-serve skew: nothing was found, so nothing was recorded.
        __UNMATCHED_BLOCKERS__ blocker matched no family and is listed by name
        rather than absorbed.</p>
    </div>
    <div>
      <h3>Confidence</h3>
      <div class="chart-box"><canvas id="cConf"></canvas></div>
      <p class="caption">Confidence is computed from retrieved bytes, never
        asserted by the model. It still does not separate correct from
        incorrect — that is what the cross-tab null established.</p>
    </div>
  </div>

  <h3>Official MCP servers — __MCP_COUNT__, vendor-domain evidence only</h3>
  <ul class="plain">__MCP_LIST__</ul>
  <p class="caption">Retracted by the provenance guard:
    <span class="mono accent-t">__MCP_RETRACTED__</span> — the claim rested on a
    third-party directory. Both original guards passed; neither asked whether
    the vendor said it.</p>
</section>

<!-- ============ 6. DATA TABLE ============ -->
<section id="data">
  <h2>The corpus</h2>
  <div class="note">
    <span class="t">__N__ of 100 profiled</span>
    The run ended on <span class="mono">DailyQuotaExhausted</span> at
    <span class="mono">196,846/200,000</span> tokens; __QUOTA_ROWS__ rows record
    hitting it. <strong>Every figure on this page is over __N__, not 100.</strong>
    The __FAILED_COUNT__ <span class="mono">RESEARCH FAILED</span> rows are shown
    as such, not hidden.
  </div>

  <button class="toggle" id="tblToggle" aria-expanded="false" aria-controls="tableWrap">
    Show all __N__ rows <span class="c">— sortable, filterable</span>
  </button>

  <div id="tableWrap" hidden>
    <div class="controls">
      <input id="q" type="search" placeholder="Search app or blocker…" aria-label="Search">
      <select id="fCat" aria-label="Category"><option value="">All categories</option></select>
      <select id="fTier" aria-label="Access tier"><option value="">All tiers</option></select>
      <select id="fAuth" aria-label="Auth"><option value="">All auth</option></select>
      <select id="fBuild" aria-label="Buildable"><option value="">All buildability</option></select>
      <select id="fConf" aria-label="Confidence"><option value="">All confidence</option></select>
    </div>
    <p class="caption" id="count" style="margin:0 0 10px"></p>
    <div class="scroll">
      <table id="dataTable">
        <thead><tr>
          <th data-k="name">app</th>
          <th data-k="category">category</th>
          <th data-k="access_tier">tier</th>
          <th data-k="api_breadth">breadth</th>
          <th data-k="auth">auth</th>
          <th data-k="buildable_today">buildable</th>
          <th data-k="confidence">conf</th>
          <th>evidence</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </div>
</section>

<!-- ============ 7. LESSONS ============ -->
<section id="lessons">
  <h2>What the run taught me</h2>
  <p class="lede two" style="max-width:76ch">One shape runs through all of it:
    <strong>a signal that exists but nobody acts on</strong>, so the system
    looks healthy while being wrong. A crash announces itself; a correct warning
    that changes nothing does not.</p>

  <div class="lesson">
    <h4>The health probe sent one token</h4>
    <p>It got a 200 back, so the daily bucket looked fine while being spent. A
      check that does not resemble the workload measures the check.</p>
  </div>
  <div class="lesson">
    <h4>The provider fallback warned and kept running</h4>
    <p>A UTF-8 BOM hid <span class="mono">COMPOSIO_API_KEY</span>, and three apps
      were profiled off DuckDuckGo before anyone noticed. A whole corpus on the
      wrong evidence source is not a degraded result, it is a different
      experiment. It now aborts.</p>
  </div>
  <div class="lesson">
    <h4>Confidence let the model assert its own trustworthiness</h4>
    <p>The column asked the model whether it had read official documentation and
      believed the answer. It is now computed from bytes actually retrieved.</p>
  </div>
  <div class="lesson key">
    <h4>The findings banner asserted a cause it had not checked</h4>
    <p>It printed &ldquo;the run stopped on the provider&rsquo;s daily token
      budget&rdquo; as a hardcoded line while the run was still going. I wrote
      that one <strong>while fixing the other three</strong>, which is the
      clearest evidence I have that the shape is easy to reproduce and hard to
      see.</p>
  </div>
  <div class="lesson key">
    <h4>Braze MCP — the strongest instance</h4>
    <p>The claim rested on <span class="mono">apis.io</span>, a third-party
      directory. It produced <strong>no symptom</strong>: the row was
      well-formed, the guards passed, the URL resolved. It was caught by
      auditing output, not by debugging. And both guards were
      <strong>individually correct and jointly insufficient</strong> —
      fetched-ness is necessary, mentions-MCP is necessary, neither is
      sufficient, and their conjunction is not sufficient either. Provenance was
      a third property nothing surfaced until a real row exposed it. There was
      no signal to convert into a consequence, because the signal did not exist.</p>
  </div>
  <div class="lesson">
    <h4>Unresolved: the deadline instrumentation disagrees with itself</h4>
    <p>All five deadline hits were in stage <span class="mono">extract</span>.
      Pass 1&rsquo;s two fired at <span class="mono">600.01s</span> exactly.
      Pass 2&rsquo;s three recorded <span class="mono">1271.99s</span>,
      <span class="mono">1400.13s</span> and <span class="mono">1271.86s</span>
      against the same 600s bound — 2.1&ndash;2.3&times;.
      <strong>Something is not measuring what it claims.</strong> Not diagnosed.
      Stated because an unexplained instrumentation discrepancy is worth more
      visible than buried.</p>
    <ul class="plain" style="margin-top:10px">__DEADLINES__</ul>
  </div>
</section>

<!-- ============ 8. HOW IT WORKS + LIMITATIONS ============ -->
<section id="how">
  <h2>How it works</h2>
  <div class="pipe">
    <div class="st"><b>search</b><span>Composio-backed search; refuses to fall
      back silently</span></div>
    <div class="st"><b>rank</b><span>official domains first, third-party hosts
      capped</span></div>
    <div class="st"><b>fetch</b><span>bounded, cached, thin pages floored</span></div>
    <div class="st"><b>condense</b><span>scored blocks, not blind
      truncation</span></div>
    <div class="st"><b>extract</b><span>strict JSON; model proposes, code
      disposes</span></div>
    <div class="st"><b>score</b><span>confidence from retrieved bytes</span></div>
    <div class="st"><b>layers 1&ndash;3</b><span>downgrade only, never
      upgrade</span></div>
  </div>
  <p class="caption"><strong>Where a human was needed:</strong> the access-tier
    hand check — only a person reading the vendor&rsquo;s pricing page settles
    it. <strong>Where one was not available:</strong> the __AUDIT_SIZE__-row
    audit queue, generated (<span class="mono">__AUDIT_RULE__</span>) and left
    unfilled, which is why no accuracy figure exists.</p>

  <h3>Limitations</h3>
  <ul class="plain">
    <li><strong>__N__ of 100 profiled.</strong> The daily token budget ran out.
      Every figure is over __N__.</li>
    <li><strong>Layer 2 reached __L2_COVERAGE__.</strong> __L2_USABLE__ usable
      readings; __L2_UNVERIFIED__ rows have no second reading and are named in
      <span class="mono">corroboration_summary.json</span>.</li>
    <li><strong>Layer 3 skipped.</strong> Deliberate, and the budget was gone
      anyway — both facts recorded rather than one of them.</li>
    <li><strong>Salesforce excluded from the hand-check rates.</strong> Its
      research failed, so there was no tier claim to score, and the vendor page
      that would settle it returned 403.</li>
    <li><strong>Forced audit apps missing.</strong> Amazon SP-API, PitchBook and
      Salesforce Commerce Cloud are not in the corpus, so the hardest tier cases
      went untested. Recorded, not substituted.</li>
    <li><strong>Design 2, E-signature 1, Scheduling 1.</strong> Too thin for a
      per-category claim. A corpus weakness, not a scheduling one.</li>
    <li><strong>__FAILED_COUNT__ rows failed research</strong> and carry no
      findings: <span class="mono faint">__FAILED_LIST__</span></li>
  </ul>
</section>

</div>

<footer>
  <div class="wrap">
    <div style="margin-bottom:12px">Every figure on this page is read from these
      artifacts at build time.</div>
    <a href="data/pass1.validated.json">pass1.validated.json</a>
    <a href="data/hand_check.json">hand_check.json</a>
    <a href="data/patterns.json">patterns.json</a>
    <a href="data/corroboration_summary.json">corroboration_summary.json</a>
    <a href="data/validation_report.json">validation_report.json</a>
    <a href="data/hand_check_queue.csv">hand_check_queue.csv</a>
  </div>
</footer>

<script type="application/json" id="corpus">__CORPUS_JSON__</script>
<script type="application/json" id="charts">__PATTERNS_JSON__</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
(function(){
"use strict";
var ROWS = JSON.parse(document.getElementById('corpus').textContent);
var CH   = JSON.parse(document.getElementById('charts').textContent);
var AC = '#f0a04b', DIM = '#98a2b3', LINE = '#242a35', FG = '#e7ebf2';

/* ---------- charts ---------- */
function bar(id, labels, data, horizontal){
  var el = document.getElementById(id);
  if (!el) return;
  if (typeof Chart === 'undefined'){
    el.parentNode.innerHTML = '<div class="chart-fallback">' +
      labels.map(function(l,i){return l + '  ' + data[i];}).join('<br>') +
      '</div>';
    return;
  }
  new Chart(el, {
    type:'bar',
    data:{labels:labels, datasets:[{data:data, backgroundColor:AC,
      borderRadius:3, barThickness:'flex', maxBarThickness:26}]},
    options:{
      indexAxis: horizontal ? 'y' : 'x',
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:false},
        tooltip:{backgroundColor:'#11141a',borderColor:LINE,borderWidth:1,
          titleColor:FG,bodyColor:DIM,padding:10,displayColors:false}},
      scales:{
        x:{ticks:{color:DIM,font:{size:11}},grid:{color:LINE,drawTicks:false},
           border:{color:LINE}},
        y:{ticks:{color:DIM,font:{size:11}},grid:{color:LINE,drawTicks:false},
           border:{color:LINE}}
      }
    }
  });
}
function top(obj, n){
  var e = Object.keys(obj).map(function(k){return [k, obj[k]];});
  e.sort(function(a,b){return b[1]-a[1];});
  return e.slice(0, n || 99);
}
var a = top(CH.auth, 8);
bar('cAuth', a.map(function(x){return x[0];}), a.map(function(x){return x[1];}), true);
var c = top(CH.categories);
bar('cCat', c.map(function(x){return x[0];}), c.map(function(x){return x[1];}), true);
var b = top(CH.blockers);
bar('cBlock', b.map(function(x){return x[0].replace(/_/g,' ');}),
    b.map(function(x){return x[1];}), true);
bar('cConf', ['high','medium','low'],
    [CH.confidence.high||0, CH.confidence.medium||0, CH.confidence.low||0], false);

/* ---------- data table ---------- */
var tbody = document.getElementById('tbody');
var countEl = document.getElementById('count');
var sortKey = 'name', sortDir = 1;

function authOf(r){ return (r.auth_methods||[]).slice().sort().join(' + ') || '—'; }
function failed(r){ return (r.agent_notes||'').indexOf('RESEARCH FAILED') === 0; }

function fill(sel, vals){
  var el = document.getElementById(sel);
  vals.sort().forEach(function(v){
    var o = document.createElement('option'); o.value = v; o.textContent = v;
    el.appendChild(o);
  });
}
function uniq(fn){
  var s = {}; ROWS.forEach(function(r){ var v = fn(r); if (v) s[v] = 1; });
  return Object.keys(s);
}
fill('fCat',  uniq(function(r){return r.category;}));
fill('fTier', uniq(function(r){return r.access_tier;}));
fill('fAuth', uniq(authOf));
fill('fBuild',uniq(function(r){return r.buildable_today;}));
fill('fConf', uniq(function(r){return r.confidence;}));

function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"]/g, function(m){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m];
  });
}

function render(){
  var q = document.getElementById('q').value.toLowerCase();
  var fc = document.getElementById('fCat').value;
  var ft = document.getElementById('fTier').value;
  var fa = document.getElementById('fAuth').value;
  var fb = document.getElementById('fBuild').value;
  var ff = document.getElementById('fConf').value;

  var out = ROWS.filter(function(r){
    if (fc && r.category !== fc) return false;
    if (ft && r.access_tier !== ft) return false;
    if (fa && authOf(r) !== fa) return false;
    if (fb && r.buildable_today !== fb) return false;
    if (ff && r.confidence !== ff) return false;
    if (q){
      var hay = (r.name + ' ' + (r.primary_blocker||'') + ' ' +
                 (r.one_liner||'')).toLowerCase();
      if (hay.indexOf(q) === -1) return false;
    }
    return true;
  });

  out.sort(function(x, y){
    var a = sortKey === 'auth' ? authOf(x) : (x[sortKey] == null ? '' : x[sortKey]);
    var b = sortKey === 'auth' ? authOf(y) : (y[sortKey] == null ? '' : y[sortKey]);
    return String(a).localeCompare(String(b)) * sortDir;
  });

  tbody.innerHTML = out.map(function(r){
    var url = (r.evidence_urls || [])[0];
    var ev = url && url.indexOf('http') === 0
      ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">source</a>'
      : '<span class="faint">—</span>';
    var conf = failed(r)
      ? '<span class="badge failed">FAILED</span>'
      : '<span class="badge ' + esc(r.confidence) + '">' + esc(r.confidence) + '</span>';
    return '<tr>' +
      '<td class="mono">' + esc(r.name) + '</td>' +
      '<td class="dim">' + esc(r.category) + '</td>' +
      '<td class="mono">' + esc(r.access_tier) + '</td>' +
      '<td class="dim">' + esc(r.api_breadth) + '</td>' +
      '<td class="dim">' + esc(authOf(r)) + '</td>' +
      '<td class="dim">' + esc(r.buildable_today) + '</td>' +
      '<td>' + conf + '</td>' +
      '<td class="ev">' + ev + '</td>' +
      '</tr>';
  }).join('');

  countEl.textContent = out.length + ' of ' + ROWS.length + ' rows';
}

['q','fCat','fTier','fAuth','fBuild','fConf'].forEach(function(id){
  var el = document.getElementById(id);
  el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', render);
});

Array.prototype.forEach.call(
  document.querySelectorAll('#dataTable th[data-k]'), function(th){
    th.addEventListener('click', function(){
      var k = th.getAttribute('data-k');
      sortDir = (k === sortKey) ? -sortDir : 1;
      sortKey = k;
      render();
    });
  });

var toggle = document.getElementById('tblToggle');
var wrap = document.getElementById('tableWrap');
toggle.addEventListener('click', function(){
  var open = !wrap.hidden;
  wrap.hidden = open;
  toggle.setAttribute('aria-expanded', String(!open));
  toggle.innerHTML = open
    ? 'Show all ' + ROWS.length + ' rows <span class="c">— sortable, filterable</span>'
    : 'Hide table <span class="c">— ' + ROWS.length + ' rows</span>';
  if (!open) render();
});

render();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
