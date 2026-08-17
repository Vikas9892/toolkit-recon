"""The downstream chain must run clean on a partial corpus.

The run is expected to stop on a provider daily-token wall somewhere short of
100 apps. Every stage after pass 1 therefore has to work on N rows and say so,
rather than assuming a denominator of 100 or a fixed sample size.

This drives the whole chain over a 40-row fixture: Layer 1 validation, Layer 2
corroboration, the audit sampler, and the progression report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toolkit_recon import audit as audit_mod  # noqa: E402
from toolkit_recon import corroborate as corr_mod  # noqa: E402
from toolkit_recon import progression as prog_mod  # noqa: E402
from toolkit_recon import validate as val_mod  # noqa: E402
from toolkit_recon.apps import APPS  # noqa: E402
from toolkit_recon.config import settings  # noqa: E402
from toolkit_recon.coverage import order_for_coverage  # noqa: E402

FIXTURE_N = 40


def _fixture_rows(n: int = FIXTURE_N) -> list[dict]:
    """N real apps from the corpus, so slugs and categories are genuine."""
    rows = []
    for i, app in enumerate(APPS[:n]):
        conf = ["high", "medium", "low"][i % 3]
        rows.append({
            "name": app.name, "category": app.category,
            "one_liner": f"{app.name} does things.",
            "auth_methods": ["OAuth2"] if i % 2 else ["OAuth2", "API Key"],
            "access_tier": "self_serve_free" if i % 3 else "paid_plan_required",
            "api_style": ["REST"], "api_breadth": "moderate",
            "has_mcp": False, "mcp_evidence_url": None,
            "buildable_today": "yes" if i % 3 else "yes_with_caveats",
            "primary_blocker": None if i % 3 else "API requires a paid plan",
            "evidence_urls": [f"https://docs.{app.slug}.example/auth"],
            "confidence": conf, "agent_notes": "", "pass_number": 1,
            "extracted_by": "openai/gpt-oss-20b",
        })
    return rows


@pytest.fixture
def partial(tmp_path, monkeypatch):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    raw = data / "raw"
    ckpt = data / "checkpoints"
    for d in (data, logs, raw, ckpt):
        d.mkdir(parents=True)
    monkeypatch.setattr(settings, "data_dir", data)
    monkeypatch.setattr(settings, "logs_dir", logs)
    monkeypatch.setattr(settings, "raw_dir", raw)
    monkeypatch.setattr(settings, "checkpoint_dir", ckpt)

    rows = _fixture_rows()
    (data / "pass1.json").write_text(json.dumps(rows), encoding="utf-8")
    (ckpt / "pass1.checkpoint.json").write_text(
        json.dumps({APPS[i].slug: r for i, r in enumerate(rows)}), encoding="utf-8")
    (logs / "trace.jsonl").write_text("", encoding="utf-8")
    return data, rows


# ---------------- Layer 1 ----------------


def test_layer1_runs_on_a_partial_corpus(partial):
    data, rows = partial
    rep = val_mod.validate(1)

    assert rep["rows_checked"] == FIXTURE_N, "must validate N, not assume 100"
    assert (data / "validation_report.json").exists()
    assert (data / "pass1.validated.json").exists()

    dist = rep["confidence_before"]
    assert sum(dist.values()) == FIXTURE_N, "distribution must sum to N"


def test_layer1_counts_are_over_n_not_100(partial):
    _, rows = partial
    rep = val_mod.validate(1)
    for info in rep["rules"].values():
        assert info["count"] <= FIXTURE_N
        assert len(info["apps"]) <= FIXTURE_N


# ---------------- Layer 2 ----------------


def test_layer2_scopes_to_the_completed_set(partial):
    data, rows = partial
    val_mod.validate(1)

    # Pass 2 covers only a subset, as a budget-bounded second pass would.
    subset = [dict(r, pass_number=2) for r in rows[:15]]
    (data / "pass2.raw.json").write_text(json.dumps(subset), encoding="utf-8")

    s = corr_mod.corroborate()
    assert s["rows_compared"] == 15, "must compare the overlap, not the corpus"
    out = json.loads((data / "pass2.json").read_text(encoding="utf-8"))
    assert len(out) == 15


def test_layer2_reports_which_rows_went_unverified(partial):
    data, rows = partial
    val_mod.validate(1)
    subset = [dict(r, pass_number=2) for r in rows[:15]]
    (data / "pass2.raw.json").write_text(json.dumps(subset), encoding="utf-8")
    corr_mod.corroborate()

    validated = json.loads((data / "pass1.validated.json").read_text(encoding="utf-8"))
    second_passed = {r["name"] for r in
                     json.loads((data / "pass2.json").read_text(encoding="utf-8"))}
    unverified = [r["name"] for r in validated if r["name"] not in second_passed]

    # The gap must be identifiable from the artifacts alone.
    assert len(unverified) == FIXTURE_N - 15
    assert set(unverified).isdisjoint(second_passed)


def test_a_failed_second_pass_is_not_counted_as_coverage(partial):
    """Attempted is not verified.

    A budget-bounded run attempts N rows and some of them fail. Counting the
    failures as second-passed reports coverage the corpus does not have, which
    is the same silent-partial-coverage failure Layer 2 exists to prevent.
    """
    data, rows = partial
    val_mod.validate(1)

    attempted = [dict(r, pass_number=2) for r in rows[:10]]
    for r in attempted[:6]:
        r["agent_notes"] = "RESEARCH FAILED: provider daily token budget exhausted"
    (data / "pass2.raw.json").write_text(json.dumps(attempted), encoding="utf-8")

    s = corr_mod.corroborate()
    assert s["rows_attempted_second_pass"] == 10
    assert s["second_pass_failed"] == 6
    assert s["rows_second_passed"] == 4          # not 10
    assert s["second_pass_coverage"] == round(4 / FIXTURE_N, 4)

    # Both ways of having no second reading are named, and neither is lost.
    assert len(s["never_attempted_rows"]) == FIXTURE_N - 10
    assert set(s["second_pass_failed_rows"]).isdisjoint(s["never_attempted_rows"])
    assert (len(s["unverified_rows"])
            == s["second_pass_failed"] + len(s["never_attempted_rows"]))
    assert s["rows_second_passed"] + len(s["unverified_rows"]) == FIXTURE_N


def test_a_failed_second_pass_never_promotes_confidence(partial):
    data, rows = partial
    val_mod.validate(1)
    attempted = [dict(r, pass_number=2,
                      agent_notes="RESEARCH FAILED: budget exhausted")
                 for r in rows[:5]]
    (data / "pass2.raw.json").write_text(json.dumps(attempted), encoding="utf-8")

    s = corr_mod.corroborate()
    assert s["confidence_promotions"] == 0


# ---------------- Phase 3 sampler ----------------


def test_sampler_derives_strata_from_n(partial):
    _, rows = partial
    picked, meta = audit_mod.sample(rows, forced=[])

    assert meta["rows_in_corpus"] == FIXTURE_N
    assert meta["corpus_is_partial"] is True
    assert meta["actual_size"] == len(picked) <= 20
    assert meta["strata"]["high"] + meta["strata"]["medium_low"] == len(picked)
    # Nothing may exceed what the corpus can supply.
    assert meta["strata"]["high"] <= sum(1 for r in rows if r["confidence"] == "high")


def test_audit_queue_writes_from_a_partial_corpus(partial):
    data, rows = partial
    picked, meta = audit_mod.sample(rows, forced=[])
    path = audit_mod.write_queue(picked, meta)
    assert path.exists()
    assert (data / "audit_sample_meta.json").exists()


# ---------------- progression ----------------


def test_progression_percentages_are_over_n(partial):
    data, rows = partial
    val_mod.validate(1)
    subset = [dict(r, pass_number=2) for r in rows[:15]]
    (data / "pass2.raw.json").write_text(json.dumps(subset), encoding="utf-8")
    corr_mod.corroborate()

    prog = prog_mod.build()
    assert prog["pass_1"]["rows"] == FIXTURE_N
    assert sum(prog["pass_1"]["confidence_distribution"].values()) == FIXTURE_N
    conv = prog["convergence"]["pass1_to_pass2"]
    assert conv["rows_compared"] == 15
    assert 0.0 <= conv["convergence_rate"] <= 1.0
    # Accuracy still withheld — a partial corpus does not license a guess.
    assert prog["accuracy"]["precision_by_confidence"] is None


def test_full_downstream_chain_runs_clean_on_40_rows(partial):
    """The whole point: every stage after pass 1, end to end, on N=40."""
    data, rows = partial

    rep = val_mod.validate(1)
    assert rep["rows_checked"] == FIXTURE_N

    subset = [dict(r, pass_number=2) for r in rows[:15]]
    (data / "pass2.raw.json").write_text(json.dumps(subset), encoding="utf-8")
    summary = corr_mod.corroborate()
    assert summary["rows_compared"] == 15

    picked, meta = audit_mod.sample(rows, forced=[])
    audit_mod.write_queue(picked, meta)

    prog = prog_mod.build()

    for name in ("validation_report.json", "pass1.validated.json", "pass2.json",
                 "disagreements.json", "corroboration_summary.json",
                 "audit_queue.csv", "audit_sample_meta.json"):
        assert (data / name).exists(), f"{name} missing"

    assert prog["pass_1"]["rows"] == FIXTURE_N
    assert prog["accuracy"]["rows_audited"] is None


# ---------------- coverage ordering ----------------


def test_coverage_ordering_levels_categories_before_deepening():
    """With budget short of the corpus, breadth beats list order."""
    pending = list(APPS)
    ordered = order_for_coverage(pending, {})

    # The opening slice must touch distinct categories rather than repeat one.
    lead = [a.category for a in ordered[:10]]
    assert len(set(lead)) == 10, f"queue front repeats categories: {lead}"


def test_coverage_ordering_respects_work_already_done():
    """Categories already covered go to the back, not the front."""
    done = {"CRM & Sales": 10, "Communication": 10}
    pending = [a for a in APPS if a.category not in done]
    ordered = order_for_coverage(pending, done)
    assert ordered[0].category not in done


def test_coverage_ordering_is_deterministic():
    a = order_for_coverage(list(APPS), {})
    b = order_for_coverage(list(APPS), {})
    assert [x.slug for x in a] == [x.slug for x in b]
