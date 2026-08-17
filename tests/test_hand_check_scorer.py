"""Hand-check scorer: does the ~90% self-serve figure survive ground truth?

No network. The queue is filled by a human against vendor pricing pages, and
the only thing this scorer may count is what that human wrote in
`truth_access_tier`. The tests below pin the three ways it could flatter the
pipeline: treating a blank truth as agreement, scoring a row whose research
failed as a gated call the pipeline earned, and reporting an extrapolation
from an enriched queue as if it were a corpus measurement.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from toolkit_recon import tier_audit  # noqa: E402
from toolkit_recon.config import settings  # noqa: E402
from toolkit_recon.tier_audit import (  # noqa: E402
    _FN_INFLATED, _FN_SURVIVES, _FN_SYSTEMATIC, _MIN_TO_CONCLUDE,
    HandCheckAlreadyFilled, hand_check_queue, score_hand_check,
)

COLS = ["slug", "name", "category", "why_selected", "agent_access_tier",
        "agent_confidence", "agent_primary_blocker", "evidence_urls",
        "truth_access_tier", "truth_source", "truth_evidence_url",
        "why_it_failed", "agent_pass_truth"]


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "hand_check_queue.csv"
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLS})
    return p


def _row(name: str, agent: str, truth: str = "", **kw) -> dict:
    return {
        "slug": name.lower(), "name": name, "category": "CRM & Sales",
        "why_selected": "expected-gated but returned self-serve",
        "agent_access_tier": agent, "agent_confidence": "medium",
        "agent_primary_blocker": "", "evidence_urls": f"https://docs.{name}.com",
        "truth_access_tier": truth,
        # Ground truth counts only when a human established it.
        "truth_source": "human" if truth else "",
        "truth_evidence_url": f"https://{name}.com/pricing" if truth else "",
        **kw,
    }


def _self_serve(n: int, truth: str, start: int = 0) -> list[dict]:
    return [_row(f"app{i}", "self_serve_free", truth)
            for i in range(start, start + n)]


# ---------------------------------------------------------------------------
# A blank truth is an unchecked row, never an agreement.
# ---------------------------------------------------------------------------


def test_unfilled_queue_scores_nothing_and_refuses_to_confirm(tmp_path):
    res = score_hand_check(_write(tmp_path, _self_serve(7, "")))
    assert res["rows_scored"] == 0
    assert len(res["rows_not_yet_checked"]) == 7
    assert res["false_negative_rate"]["rate"] is None
    assert "unverified" in res["verdict"]
    # The dangerous failure: silence read as vindication.
    assert "survives" not in res["verdict"].lower()


def test_partially_filled_queue_counts_only_filled_rows(tmp_path):
    rows = _self_serve(3, "paid_plan_required") + _self_serve(4, "", start=3)
    res = score_hand_check(_write(tmp_path, rows))
    assert res["rows_scored"] == 3
    assert res["false_negative_rate"]["checked"] == 3
    assert len(res["rows_not_yet_checked"]) == 4


def test_below_minimum_sample_does_not_conclude(tmp_path):
    n = _MIN_TO_CONCLUDE - 1
    res = score_hand_check(_write(tmp_path, _self_serve(n, "paid_plan_required")))
    # Every single checked row was wrong, and it still must not conclude.
    assert res["false_negative_rate"]["rate"] == 1.0
    assert "below the" in res["verdict"]
    assert "SYSTEMATIC" not in res["verdict"]


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


def test_false_negative_rate_counts_gated_truth_on_self_serve_claims(tmp_path):
    rows = _self_serve(6, "paid_plan_required") + _self_serve(2, "self_serve_free",
                                                              start=6)
    res = score_hand_check(_write(tmp_path, rows))
    fn = res["false_negative_rate"]
    assert (fn["wrong"], fn["checked"]) == (6, 8)
    assert fn["rate"] == 0.75
    assert res["verdict"].startswith("SYSTEMATIC BIAS")
    assert "does not survive" in res["verdict"]


def test_false_positive_rate_is_separate_from_false_negative(tmp_path):
    rows = _self_serve(6, "self_serve_free")
    rows += [_row("gated1", "partner_gated", "self_serve_free"),
             _row("gated2", "partner_gated", "partner_gated")]
    res = score_hand_check(_write(tmp_path, rows))
    assert res["false_negative_rate"]["rate"] == 0.0
    assert res["false_positive_rate"] == {
        "definition": res["false_positive_rate"]["definition"],
        "checked": 2, "wrong": 1, "rate": 0.5,
    }
    # A clean self-serve read does not excuse dirty gating reads.
    assert "gating verdicts are not" in res["verdict"]


def test_wrong_tier_on_the_right_side_of_the_line_is_not_an_error_rate(tmp_path):
    # self_serve_free vs self_serve_trial is a mistake, but not the mistake the
    # headline is about, so it must not inflate the false-negative rate.
    rows = _self_serve(6, "self_serve_trial")
    res = score_hand_check(_write(tmp_path, rows))
    assert res["false_negative_rate"]["rate"] == 0.0
    assert res["same_class_mismatches"] == 6
    assert all(a["outcome"] == "same_class_mismatch" for a in res["per_app"])


def test_research_failures_are_excluded_from_both_rates(tmp_path):
    rows = _self_serve(6, "self_serve_free")
    rows.append(_row("salesforce", "no_public_api", "self_serve_free",
                     agent_primary_blocker="research failed; not assessed",
                     evidence_urls="unresolved://salesforce"))
    res = score_hand_check(_write(tmp_path, rows))
    # The pipeline never made a tier claim here, so it may not be scored as one.
    assert res["false_positive_rate"]["checked"] == 0
    assert res["excluded_research_failures"] == ["salesforce"]
    excluded = [a for a in res["per_app"] if a["app"] == "salesforce"][0]
    assert "never made a tier claim" in excluded["excluded_from_rates"]


# ---------------------------------------------------------------------------
# Verdict bands
# ---------------------------------------------------------------------------


def _rate_of(tmp_path, wrong: int, total: int) -> str:
    rows = (_self_serve(wrong, "paid_plan_required")
            + _self_serve(total - wrong, "self_serve_free", start=wrong))
    return score_hand_check(_write(tmp_path, rows))["verdict"]


def test_verdict_bands(tmp_path):
    assert _rate_of(tmp_path, 0, 20).startswith("SURVIVES")
    assert _rate_of(tmp_path, 16, 20).startswith("SYSTEMATIC BIAS")
    assert _rate_of(tmp_path, 6, 20).startswith("PARTIALLY REAL")
    assert _rate_of(tmp_path, 4, 20).startswith("UNRESOLVED")


def test_band_thresholds_are_ordered(tmp_path):
    assert _FN_SURVIVES < _FN_INFLATED < _FN_SYSTEMATIC


# ---------------------------------------------------------------------------
# Per-app evidence, projection, and the reason this test exists at all
# ---------------------------------------------------------------------------


def test_per_app_carries_vendor_url_and_why_it_failed(tmp_path):
    rows = _self_serve(5, "paid_plan_required")
    rows[0]["why_it_failed"] = "docs describe the API; the gate is on /pricing"
    res = score_hand_check(_write(tmp_path, rows))
    a = res["per_app"][0]
    assert a["vendor_evidence_url"] == "https://app0.com/pricing"
    assert a["why_it_failed"].startswith("docs describe the API")
    assert a["outcome"] == "false_negative"


def test_verdict_without_a_vendor_url_is_flagged_as_asserted(tmp_path):
    rows = _self_serve(5, "paid_plan_required")
    for r in rows:
        r["truth_evidence_url"] = ""
    res = score_hand_check(_write(tmp_path, rows))
    assert all("asserted, not cited" in a["evidence_note"] for a in res["per_app"])


def test_invalid_truth_value_is_rejected_not_coerced(tmp_path):
    rows = _self_serve(5, "gated")  # not a member of the enum
    res = score_hand_check(_write(tmp_path, rows))
    assert [b["value"] for b in res["invalid_truth_values"]] == ["gated"] * 5
    assert res["rows_scored"] == 0
    assert res["false_negative_rate"]["rate"] is None


def test_projection_is_absent_without_corpus_and_labelled_worst_case(tmp_path):
    rows = _self_serve(10, "paid_plan_required")
    csv_path = _write(tmp_path, rows)

    assert score_hand_check(csv_path)["corpus_projection"]["available"] is False

    corpus = [{"access_tier": "self_serve_free"}] * 90 + \
             [{"access_tier": "partner_gated"}] * 10
    proj = score_hand_check(csv_path, corpus)["corpus_projection"]
    assert proj["observed_self_serve_share"] == 0.9
    assert proj["worst_case_self_serve_share"] == 0.0  # 100% miss rate applied
    assert "never as a measurement" in proj["caveat"]


def test_only_a_human_verdict_counts_as_ground_truth(tmp_path):
    """An agent-filled truth is retained but never scored.

    A second model reading the same class of page is not independent of the
    first, so counting its verdicts as ground truth would launder a model's
    opinion into a measurement.
    """
    rows = _self_serve(3, "paid_plan_required")
    rows += [_row(f"agentfilled{i}", "self_serve_free", "paid_plan_required",
                  truth_source="") for i in range(4)]
    res = score_hand_check(_write(tmp_path, rows))
    assert res["false_negative_rate"]["checked"] == 3      # not 7
    assert len(res["rows_not_yet_checked"]) == 4


def test_schema_gap_rows_are_excluded_from_both_rates_and_named(tmp_path):
    """Some rows have no correct answer because the question is malformed."""
    rows = _self_serve(3, "paid_plan_required")
    rows.append(_row("bill", "self_serve_free", "schema_cannot_express"))
    res = score_hand_check(_write(tmp_path, rows))

    assert res["false_negative_rate"]["checked"] == 3      # BILL not counted
    assert res["schema_cannot_express"]["count"] == 1
    assert res["schema_cannot_express"]["rows"][0]["app"] == "bill"
    assert "question is malformed" in res["schema_cannot_express"]["note"]
    # It must not be silently downgraded into an invalid-vocabulary complaint.
    assert res["invalid_truth_values"] == []


def test_agent_filled_pass_is_scored_against_the_human(tmp_path):
    rows = [
        _row("salesloft", "self_serve_free", "partner_gated",
             agent_pass_truth="paid_plan_required"),
        _row("gorgias", "self_serve_free", "self_serve_free",
             agent_pass_truth="paid_plan_required"),
        _row("deel", "self_serve_free", "self_serve_free",
             agent_pass_truth="paid_plan_required"),
        _row("bill", "self_serve_free", "schema_cannot_express",
             agent_pass_truth="paid_plan_required"),
    ]
    res = score_hand_check(_write(tmp_path, rows))
    ap = res["agent_filled_pass_vs_human"]

    assert ap["compared"] == 4
    assert ap["disagreed_with_human"] == 2
    assert sorted(d["app"] for d in ap["disagreements"]) == ["deel", "gorgias"]
    assert "not independent verification" in ap["finding"]


def test_projection_is_suppressed_below_the_sample_size_for_a_rate(tmp_path):
    """Three rows cannot support a corpus-wide magnitude, so none is offered."""
    corpus = [{"access_tier": "self_serve_free"}] * 48 + \
             [{"access_tier": "partner_gated"}] * 15
    res = score_hand_check(_write(tmp_path, _self_serve(3, "partner_gated")),
                           corpus)
    proj = res["corpus_projection"]
    assert proj["available"] is False
    assert "below the" in proj["reason"]
    assert "cannot support" in proj["reason"]
    # Direction is still stated; only magnitude is withheld.
    assert res["verdict"].startswith("DIRECTION ONLY")
    assert "no magnitude is claimed" in res["verdict"]


def test_provenance_is_reported_and_unrecorded_is_not_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    res = score_hand_check(_write(tmp_path, _self_serve(5, "self_serve_free")))
    # No meta file: the check must say the independence is unknown, not assume it.
    assert res["filled_by"]["who"] == "unrecorded"

    (tmp_path / "hand_check_meta.json").write_text(
        '{"filled_by": {"who": "a human, against vendor pricing pages"}}',
        encoding="utf-8")
    res = score_hand_check(_write(tmp_path, _self_serve(5, "self_serve_free")))
    assert res["filled_by"]["who"] == "a human, against vendor pricing pages"


# ---------------------------------------------------------------------------
# A filled queue is the one artefact here that cannot be regenerated.
# ---------------------------------------------------------------------------


def _corpus_row(name: str) -> dict:
    return {"name": name, "category": "CRM & Sales",
            "access_tier": "partner_gated", "confidence": "medium",
            "primary_blocker": "x", "evidence_urls": ["https://x.com"]}


def test_regenerating_over_a_filled_queue_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    corpus = [_corpus_row("Outreach")]

    path, _ = hand_check_queue(corpus)          # first write: nothing to lose
    assert path.exists()

    hand_check_queue(corpus)                    # still empty: still fine

    rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    rows[0]["truth_access_tier"] = "paid_plan_required"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    with pytest.raises(HandCheckAlreadyFilled) as e:
        hand_check_queue(corpus)
    assert "--hand-check-score" in str(e.value)

    # The fill survived the refusal.
    after = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    assert after[0]["truth_access_tier"] == "paid_plan_required"

    # --force is the documented escape hatch, and it really does discard.
    hand_check_queue(corpus, force=True)
    forced = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    assert forced[0]["truth_access_tier"] == ""


def test_queue_has_a_column_for_the_vendor_url(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path, _ = hand_check_queue([_corpus_row("Outreach")])
    header = path.read_text(encoding="utf-8-sig").splitlines()[0]
    # A verdict without a citation is the failure this whole module is about.
    assert "truth_evidence_url" in header
    assert tier_audit._filled_truth_count(path) == 0


def test_output_states_why_the_cross_tab_could_not_settle_this(tmp_path):
    res = score_hand_check(_write(tmp_path, _self_serve(5, "self_serve_free")))
    note = res["why_this_test_and_not_the_cross_tab"]
    assert "BY CONSTRUCTION" in note
    assert "uniform across confidence cohorts" in note
    assert "obtainable credentials" in note
    assert "Not a random sample" in res["sample_note"]
    # The sample bounds direction, never magnitude, and must say which.
    assert "DIRECTION" in res["sample_note"]
    assert "not its magnitude" in res["sample_note"]
