"""End-to-end tests for the layer file I/O, against a temp data dir.

The unit tests cover the decision logic; these cover the wiring — that each
layer reads what the previous one wrote, and that the artifacts a reviewer
opens actually contain what the README promises.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toolkit_recon import corroborate as corr_mod  # noqa: E402
from toolkit_recon import progression as prog_mod  # noqa: E402
from toolkit_recon.config import settings  # noqa: E402


def _row(name: str, **kw) -> dict:
    base = dict(
        name=name, category="CRM", one_liner="x",
        auth_methods=["OAuth2"], access_tier="self_serve_free",
        api_style=["REST"], api_breadth="moderate", has_mcp=False,
        mcp_evidence_url=None, buildable_today="yes", primary_blocker=None,
        evidence_urls=[f"https://docs.{name.lower()}.com/auth"],
        confidence="medium", agent_notes="", pass_number=1,
    )
    base.update(kw)
    return base


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


def _write(d: Path, name: str, payload) -> None:
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


# ---------------- Layer 2 wiring ----------------


def test_full_agreement_promotes_and_writes_no_disputes(data_dir):
    _write(data_dir, "pass1.validated.json", [_row("Acme", confidence="medium")])
    _write(data_dir, "pass2.raw.json", [_row("Acme", confidence="medium",
                                             pass_number=2)])

    s = corr_mod.corroborate()

    assert s["rows_compared"] == 1
    assert s["fully_agreeing_rows"] == 1
    assert s["disputed_rows"] == 0
    assert s["confidence_promotions"] == 1

    out = json.loads((data_dir / "pass2.json").read_text(encoding="utf-8"))
    assert out[0]["confidence"] == "high"       # medium -> high
    assert out[0]["pass_number"] == 2
    assert "layer2: corroborated" in out[0]["agent_notes"]
    assert json.loads((data_dir / "disagreements.json").read_text()) == []


def test_disagreement_routes_to_layer3_without_promoting(data_dir):
    _write(data_dir, "pass1.validated.json",
           [_row("Acme", access_tier="self_serve_free", confidence="medium")])
    _write(data_dir, "pass2.raw.json",
           [_row("Acme", access_tier="paid_plan_required", confidence="medium",
                 pass_number=2)])

    s = corr_mod.corroborate()

    assert s["disputed_rows"] == 1
    assert s["confidence_promotions"] == 0
    assert s["disagreements_by_field"] == {"access_tier": 1}
    assert s["layer3_queue"] == ["Acme"]

    out = json.loads((data_dir / "pass2.json").read_text(encoding="utf-8"))
    assert out[0]["confidence"] == "medium"  # not promoted, not demoted
    assert "routed to layer 3" in out[0]["agent_notes"]

    disputes = json.loads((data_dir / "disagreements.json").read_text())
    assert disputes[0]["field"] == "access_tier"
    assert disputes[0]["pass1"] == "self_serve_free"
    assert disputes[0]["pass2"] == "paid_plan_required"


def test_failed_row_is_never_promoted(data_dir):
    """A run failure produces identical placeholder rows in both passes. That
    is agreement in form only and must not buy confidence."""
    failed = _row("Acme", confidence="low",
                  agent_notes="RESEARCH FAILED: boom",
                  evidence_urls=["unresolved://acme"])
    _write(data_dir, "pass1.validated.json", [failed])
    _write(data_dir, "pass2.raw.json", [dict(failed, pass_number=2)])

    s = corr_mod.corroborate()

    assert s["confidence_promotions"] == 0
    out = json.loads((data_dir / "pass2.json").read_text(encoding="utf-8"))
    assert out[0]["confidence"] == "low"


def test_rows_absent_from_pass2_are_not_compared(data_dir):
    """Pass 2 is deliberately narrower; pass-1-only rows must be left alone."""
    _write(data_dir, "pass1.validated.json", [_row("Acme"), _row("Beta")])
    _write(data_dir, "pass2.raw.json", [_row("Acme", pass_number=2)])

    s = corr_mod.corroborate()
    assert s["rows_compared"] == 1
    names = [r["name"] for r in
             json.loads((data_dir / "pass2.json").read_text(encoding="utf-8"))]
    assert names == ["Acme"]


def test_promotion_uses_the_weaker_of_the_two_confidences(data_dir):
    """Corroboration lifts the floor by one; it cannot inherit the ceiling."""
    _write(data_dir, "pass1.validated.json", [_row("Acme", confidence="low")])
    _write(data_dir, "pass2.raw.json", [_row("Acme", confidence="high",
                                             pass_number=2)])

    corr_mod.corroborate()
    out = json.loads((data_dir / "pass2.json").read_text(encoding="utf-8"))
    assert out[0]["confidence"] == "medium"  # min(low, high)=low -> medium


# ---------------- progression wiring ----------------


def test_progression_reports_records_but_withholds_accuracy(data_dir):
    _write(data_dir, "pass1.json", [_row("Acme", confidence="high"),
                                    _row("Beta", confidence="medium")])
    _write(data_dir, "corroboration_summary.json",
           {"fully_agreeing_rows": 1, "disputed_rows": 1,
            "confidence_promotions": 1, "total_field_disagreements": 2})
    _write(data_dir, "validation_report.json",
           {"rows_with_violations": 1, "total_violations": 3,
            "confidence_before": {"high": 2, "medium": 0, "low": 0},
            "confidence_after": {"high": 1, "medium": 1, "low": 0}})

    prog = prog_mod.build()

    # Matters of record are populated...
    assert prog["pass_1"]["rows"] == 2
    assert prog["pass_1"]["high_conf_rows"] == 1
    assert prog["pass_1"]["flagged"] == 1
    assert prog["pass_2"]["resolved"] == 1
    assert prog["structural_effects"]["layer1_violations"] == 3

    # ...but accuracy is not, and must not be inferred from them.
    for k in ("pass_1", "pass_2", "pass_3"):
        assert prog[k]["accuracy"] is None
        assert prog[k]["correct"] is None


def test_progression_populates_from_a_human_audit(data_dir):
    right = _row("Acme", access_tier="self_serve_free")
    wrong = _row("Beta", access_tier="self_serve_free")
    _write(data_dir, "pass1.json", [right, wrong])
    _write(data_dir, "human_audit.json", [
        {"name": "Acme", "access_tier": "self_serve_free",
         "auth_methods": ["OAuth2"], "api_style": ["REST"],
         "api_breadth": "moderate", "has_mcp": False, "buildable_today": "yes"},
        {"name": "Beta", "access_tier": "paid_plan_required",
         "auth_methods": ["OAuth2"], "api_style": ["REST"],
         "api_breadth": "moderate", "has_mcp": False, "buildable_today": "yes"},
    ])

    prog = prog_mod.build()
    assert prog["sample_size"] == 2
    assert prog["pass_1"]["correct"] == 1
    assert prog["pass_1"]["scored_against"] == 2
    assert prog["pass_1"]["accuracy"] == 0.5


def test_audit_template_keeps_high_confidence_controls(data_dir):
    """Sampling only doubted rows would hide systematic overconfidence."""
    rows = ([_row(f"Hi{i}", confidence="high") for i in range(6)]
            + [_row(f"Lo{i}", confidence="low") for i in range(6)])
    _write(data_dir, "pass1.validated.json", rows)
    _write(data_dir, "disagreements.json", [])

    prog_mod.write_template(12)
    tmpl = json.loads(
        (data_dir / prog_mod.AUDIT_TEMPLATE_NAME).read_text(encoding="utf-8"))

    assert len(tmpl) == 12
    confs = {t["_confidence"] for t in tmpl}
    assert "high" in confs and "low" in confs
    # Ground-truth fields ship blank for the human to fill.
    assert all(t["access_tier"] is None for t in tmpl)
    # ...alongside what the pipeline claimed, so the auditor can compare.
    assert all("_pipeline_said" in t for t in tmpl)


def test_audit_template_puts_disputed_rows_first(data_dir):
    _write(data_dir, "pass1.validated.json",
           [_row("Calm", confidence="high"), _row("Contested", confidence="high")])
    _write(data_dir, "disagreements.json",
           [{"app": "Contested", "field": "access_tier",
             "pass1": "a", "pass2": "b"}])

    prog_mod.write_template(2)
    tmpl = json.loads(
        (data_dir / prog_mod.AUDIT_TEMPLATE_NAME).read_text(encoding="utf-8"))
    assert tmpl[0]["name"] == "Contested"
    assert tmpl[0]["_disputed"] is True
