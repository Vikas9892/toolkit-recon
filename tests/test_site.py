"""The HTML deliverable is generated from the artifacts, never hand-written.

The risk this file guards is a page that keeps rendering after the artifacts
behind it change -- a stale figure on a deliverable is worse than a missing
one, because a reader cannot tell it is stale. So: every number must come from
data/, no placeholder may survive the build, and the claims the page makes
about its own limits must actually hold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toolkit_recon import site as site_mod  # noqa: E402
from toolkit_recon.config import settings  # noqa: E402


def _row(name: str, **kw) -> dict:
    base = {
        "name": name, "category": "CRM & Sales", "one_liner": "x",
        "auth_methods": ["OAuth2"], "access_tier": "self_serve_free",
        "api_style": ["REST"], "api_breadth": "moderate", "has_mcp": False,
        "mcp_evidence_url": None, "buildable_today": "yes",
        "primary_blocker": None, "evidence_urls": ["https://docs.x.com/a"],
        "confidence": "medium", "agent_notes": "", "pass_number": 1,
        "extracted_by": "test-model",
    }
    base.update(kw)
    return base


@pytest.fixture
def built(tmp_path, monkeypatch):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    monkeypatch.setattr(settings, "data_dir", data)
    monkeypatch.setattr(settings, "logs_dir", logs)

    rows = [_row("Alpha"), _row("Beta", access_tier="no_public_api",
                                api_breadth="narrow", buildable_today="no"),
            _row("Gamma", agent_notes="RESEARCH FAILED: budget exhausted")]
    (data / "pass1.validated.json").write_text(json.dumps(rows), encoding="utf-8")
    (data / "hand_check.json").write_text(json.dumps({
        "false_negative_rate": {"checked": 3, "wrong": 1, "rate": 0.3333},
        "false_positive_rate": {"checked": 4, "wrong": 0, "rate": 0.0},
        "agent_filled_pass_vs_human": {
            "compared": 4, "disagreed_with_human": 2,
            "disagreements": [{"app": "Gorgias"}, {"app": "Deel"}]},
        "schema_cannot_express": {"count": 1, "rows": [
            {"app": "BILL", "agent_access_tier": "self_serve_free",
             "vendor_evidence_url": "https://www.bill.com/pricing",
             "why": "two products, two tiers"}]},
        "per_app": [
            {"app": "Salesloft", "agent_access_tier": "self_serve_free",
             "truth_access_tier": "partner_gated", "outcome": "false_negative",
             "vendor_evidence_url": "https://www.salesloft.com/pricing",
             "why_it_failed": "routes to sales"},
            {"app": "Gorgias", "agent_access_tier": "self_serve_free",
             "truth_access_tier": "self_serve_free", "outcome": "correct",
             "vendor_evidence_url": "https://www.gorgias.com/pricing",
             "why_it_failed": "pipeline correct"},
        ],
        "verdict": "DIRECTION ONLY.", "sample_note": "3 scoreable rows.",
    }), encoding="utf-8")
    (data / "patterns.json").write_text(json.dumps({
        "access_tier_caveat": {"note": "caveat text"},
        "blocker_families": {"families": {"paid_plan_required": {"count": 2}},
                             "unmatched": [], "rows_with_no_blocker_stated": 1},
    }), encoding="utf-8")
    (data / "corroboration_summary.json").write_text(json.dumps({
        "rows_attempted_second_pass": 11, "rows_second_passed": 4,
        "second_pass_failed": 7, "never_attempted_rows": ["x"] * 52,
        "second_pass_coverage": 0.0635, "total_field_disagreements": 25,
        "rows_not_second_passed": 59, "confidence_promotions": 0,
    }), encoding="utf-8")
    (data / "validation_report.json").write_text(json.dumps({
        "rows_checked": 3, "total_violations": 2}), encoding="utf-8")
    (data / "audit_sample_meta.json").write_text(json.dumps({
        "actual_size": 19, "size_derivation": {"rule": "30% of corpus"}}),
        encoding="utf-8")
    (logs / "trace.jsonl").write_text(json.dumps({
        "slug": "alpha", "pass_number": 1, "stage": "extract",
        "wall_time_s": 600.01, "deadline_hit": True, "error": ""}) + "\n",
        encoding="utf-8")

    out = tmp_path / "site"
    path = site_mod.build(out)
    return path, path.read_text(encoding="utf-8")


def test_no_placeholder_survives_the_build(built):
    _, html = built
    import re
    leftover = re.findall(r"__[A-Z_]+__", html)
    assert leftover == [], f"unreplaced placeholders: {set(leftover)}"


def test_every_headline_number_comes_from_the_artifacts(built):
    _, html = built
    # 3 rows in, 3 rows reported -- not a hardcoded 63.
    assert "3 of 100 profiled" in html
    assert "1 of 3" in html or ">1<" in html
    # The corpus payload is the corpus, not a sample of it.
    m = html.split('id="corpus">')[1].split("</script>")[0]
    assert len(json.loads(m)) == 3


def test_no_accuracy_figure_appears(built):
    """The audit queue was never filled, so no accuracy number may exist."""
    _, html = built
    import re
    assert not re.search(r"accuracy[^.<]{0,40}\d+(\.\d+)?\s*%", html, re.I)
    assert "No accuracy number appears on this page" in html


def test_misses_are_not_hidden(built):
    _, html = built
    assert ">MISS<" in html
    assert "Salesloft" in html
    assert "partner_gated" in html
    # And the schema gap is shown as its own verdict, not folded into a miss.
    assert ">UNREPRESENTABLE<" in html


def test_the_agent_pass_correction_is_stated(built):
    _, html = built
    assert "2 of 4 wrong" in html
    assert "not\n    independent verification" in html or \
           "not independent verification" in html.replace("\n    ", " ")


def test_failed_rows_are_shown_not_dropped(built):
    _, html = built
    payload = json.loads(html.split('id="corpus">')[1].split("</script>")[0])
    assert any(r["agent_notes"].startswith("RESEARCH FAILED") for r in payload)
    assert "RESEARCH FAILED" in html


def test_raw_artifacts_are_copied_next_to_the_page(built):
    path, html = built
    data_dir = path.parent / "data"
    assert (data_dir / "pass1.validated.json").exists()
    assert (data_dir / "hand_check.json").exists()
    # And the page links to them, so every figure is one click from its source.
    assert 'href="data/hand_check.json"' in html


def test_page_loads_no_external_resource_except_the_chart_cdn(built):
    """Sub-resources, not hyperlinks.

    Citation links to vendor pages are the point of the page and must stay.
    What must not creep in is a *loaded* asset -- a stylesheet, font or script
    -- since anything the page pulls at render time is something that can
    disappear and take a section of the deliverable with it.
    """
    _, html = built
    import re
    loaded = re.findall(r'<(?:script|img|iframe)[^>]+src="(https?://[^"]+)"', html)
    loaded += re.findall(r'<link[^>]+href="(https?://[^"]+)"', html)
    assert all("cdn.jsdelivr.net/npm/chart.js" in u for u in loaded), loaded
    assert len(loaded) == 1

    # Vendor citations are hyperlinks and are expected to be present.
    cites = re.findall(r'<a href="(https?://[^"]+)"', html)
    assert any("salesloft.com" in c for c in cites)


def test_charts_degrade_when_the_cdn_is_unreachable(built):
    """Opened as a local file with no network, the page must still be readable."""
    _, html = built
    # The guard that swaps a canvas for plain numbers when Chart is undefined.
    assert "typeof Chart === 'undefined'" in html
    assert "chart-fallback" in html


def test_a_missing_artifact_does_not_crash_the_build(tmp_path, monkeypatch):
    """A missing input must degrade, not explode -- and not render a fake zero."""
    data, logs = tmp_path / "data", tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    monkeypatch.setattr(settings, "data_dir", data)
    monkeypatch.setattr(settings, "logs_dir", logs)
    (data / "pass1.validated.json").write_text(json.dumps([_row("Solo")]),
                                               encoding="utf-8")
    path = site_mod.build(tmp_path / "site")
    assert path.exists()
    assert "__" not in path.read_text(encoding="utf-8").split("<style>")[0]
