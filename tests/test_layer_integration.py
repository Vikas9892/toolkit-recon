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


def test_checkpoint_never_erases_earlier_progress(tmp_path, monkeypatch):
    """Regression: a narrow run must not clobber a wide one.

    Loading used to be tied to --resume while writing was not, so a 3-app
    diagnostic wrote its 3 rows over 52 rows of completed work.
    """
    import asyncio

    from toolkit_recon.schema import AppResearch
    from toolkit_recon.storage import Checkpoint

    monkeypatch.setattr(settings, "checkpoint_dir", tmp_path)

    wide = Checkpoint(1)
    row = AppResearch.model_validate(_row("Acme"))
    asyncio.run(wide.record("acme", row))
    asyncio.run(wide.record("beta", AppResearch.model_validate(_row("Beta"))))
    assert len(json.loads(wide.path.read_text(encoding="utf-8"))) == 2

    # A second, narrower run that never calls load() explicitly.
    narrow = Checkpoint(1)
    asyncio.run(narrow.record("gamma", AppResearch.model_validate(_row("Gamma"))))

    on_disk = json.loads(narrow.path.read_text(encoding="utf-8"))
    assert set(on_disk) == {"acme", "beta", "gamma"}, "earlier rows were erased"


def test_progression_reports_records_but_withholds_accuracy(data_dir):
    _write(data_dir, "pass1.json", [_row("Acme", confidence="high"),
                                    _row("Beta", confidence="medium")])
    _write(data_dir, "corroboration_summary.json",
           {"rows_compared": 2, "fully_agreeing_rows": 1, "disputed_rows": 1,
            "confidence_promotions": 1, "total_field_disagreements": 2})
    _write(data_dir, "validation_report.json",
           {"rows_with_violations": 1, "total_violations": 3,
            "confidence_before": {"high": 2, "medium": 0, "low": 0},
            "confidence_after": {"high": 1, "medium": 1, "low": 0}})

    prog = prog_mod.build()

    # Matters of record are populated...
    assert prog["pass_1"]["rows"] == 2
    assert prog["pass_1"]["high_conf_rows"] == 1
    assert prog["pass_1"]["flagged_by_layer1"] == 1
    assert prog["pass_2"]["corroborated"] == 1
    assert prog["structural_effects"]["layer1_violations"] == 3
    assert prog["convergence"]["pass1_to_pass2"]["convergence_rate"] == 0.5

    # ...but accuracy is not, and must not be inferred from them.
    assert prog["accuracy"]["precision_by_confidence"] is None
    assert prog["accuracy"]["rows_audited"] is None
