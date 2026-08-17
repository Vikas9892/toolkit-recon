"""Phase 3 audit harness: stratified sampler + precision scorer.

No network. The sampler must be reproducible and must not quietly relax the
constraints that make the sample defensible; the scorer must not flatter the
agent by miscounting unverifiable fields.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toolkit_recon import audit as audit_mod  # noqa: E402
from toolkit_recon.config import settings  # noqa: E402
from toolkit_recon.report import VERDICT_FIELDS, score_audit  # noqa: E402

CATEGORIES = ["CRM & Sales", "Communication", "Project Management",
              "Developer Tools", "Marketing", "Customer Support",
              "HR & Recruiting", "Finance & Accounting", "Analytics & Data",
              "E-commerce"]


def _row(name: str, category: str, confidence: str) -> dict:
    return {
        "name": name, "category": category, "confidence": confidence,
        "one_liner": "x", "auth_methods": ["OAuth2"],
        "access_tier": "self_serve_free", "api_style": ["REST"],
        "api_breadth": "moderate", "has_mcp": False, "mcp_evidence_url": None,
        "buildable_today": "yes", "primary_blocker": None,
        "evidence_urls": [f"https://docs.{name.lower()}.com/auth"],
        "agent_notes": "", "pass_number": 1,
    }


def _corpus(per_cat: int = 6) -> list[dict]:
    rows = []
    for c in CATEGORIES:
        for i in range(per_cat):
            conf = "high" if i % 2 == 0 else ("medium" if i % 3 else "low")
            rows.append(_row(f"{c.split()[0]}{i}", c, conf))
    return rows


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


# ---------------- sampler ----------------


def test_sample_size_is_derived_from_the_corpus_not_fixed():
    """A fixed 20 is a different sample against 63 rows than against 100.

    It silently becomes a larger share of a smaller corpus while still reading
    as "20 rows", so the size has to move with N.
    """
    corpus = _corpus()                      # 10 categories x 6 = 60 rows
    expected = audit_mod.derive_size(len(corpus))
    picked, meta = audit_mod.sample(corpus, forced=[])
    assert len(picked) == expected
    assert meta["actual_size"] == expected
    assert meta["size_derivation"]["derived_size"] == expected
    assert meta["size_derivation"]["was_overridden"] is False


def test_derived_size_scales_with_n_and_respects_its_bounds():
    share, cap = audit_mod.SAMPLE_SHARE, audit_mod.MAX_SIZE
    floor = audit_mod.MIN_PER_STRATUM * 2

    assert audit_mod.derive_size(60) == round(share * 60)
    assert audit_mod.derive_size(63) == round(share * 63)
    # Monotonic in N: a bigger corpus never yields a smaller sample.
    sizes = [audit_mod.derive_size(n) for n in range(1, 200)]
    assert sizes == sorted(sizes)
    # Floor protects the high-vs-weak comparison; ceiling protects the human.
    assert audit_mod.derive_size(12) == floor
    assert audit_mod.derive_size(1000) == cap
    # Can never ask for more rows than exist.
    assert audit_mod.derive_size(3) == 3
    assert audit_mod.derive_size(0) == 0


def test_strata_split_evenly_when_both_cohorts_can_supply():
    picked, meta = audit_mod.sample(_corpus(), forced=[])
    n = meta["actual_size"]
    assert meta["strata"]["high"] == n // 2
    assert meta["strata"]["medium_low"] == n - n // 2
    assert all(r["confidence"] == "high"
               for r in picked if r["confidence"] == "high")


def test_no_category_is_over_represented():
    picked, meta = audit_mod.sample(_corpus(), forced=[])
    counts = {}
    for r in picked:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    cap = meta["per_category_cap"]
    assert max(counts.values()) <= cap
    # At the cap, the sample must be spread over at least this many categories.
    assert len(counts) >= meta["actual_size"] // cap


def test_sampling_is_deterministic_for_a_seed():
    a, _ = audit_mod.sample(_corpus(), seed=7, forced=[])
    b, _ = audit_mod.sample(_corpus(), seed=7, forced=[])
    assert [r["name"] for r in a] == [r["name"] for r in b]


def test_different_seeds_give_different_samples():
    a, _ = audit_mod.sample(_corpus(), seed=1, forced=[])
    b, _ = audit_mod.sample(_corpus(), seed=2, forced=[])
    assert [r["name"] for r in a] != [r["name"] for r in b]


def test_forced_apps_are_always_included():
    corpus = _corpus()
    corpus.append(_row("Amazon Selling Partner API", "E-commerce", "low"))
    picked, meta = audit_mod.sample(corpus, forced=["Amazon SP-API"])
    names = [r["name"] for r in picked]
    assert "Amazon Selling Partner API" in names
    assert meta["forced_included"] == ["Amazon Selling Partner API"]
    assert meta["forced_missing"] == []
    assert len(picked) == audit_mod.derive_size(len(corpus))


def test_missing_forced_apps_are_reported_not_substituted():
    """The three gated products are not in the 100-app corpus. Silently
    swapping in an easier app would hide that the hardest field went
    untested."""
    _, meta = audit_mod.sample(_corpus(), forced=["PitchBook"])
    assert meta["forced_missing"] == ["PitchBook"]
    assert meta["forced_included"] == []
    assert "not in the 100-app corpus" in meta["forced_missing_note"]


def test_works_on_partial_data_with_targets_derived_from_n():
    """A quota-stopped run yields fewer rows than the corpus. Strata targets
    must come from what exists, not from a hardcoded 10/10 that would either
    fail or silently over-sample one stratum."""
    tiny = [_row(f"A{i}", CATEGORIES[i % 3], "medium") for i in range(6)]
    picked, meta = audit_mod.sample(tiny, forced=[])

    assert len(picked) == 6                    # cannot invent rows
    assert meta["strata"]["high"] == 0
    assert meta["strata"]["medium_low"] == 6
    # Targeting 0 high when 0 exist is correct, not a shortfall.
    assert meta["strata_targets"]["high"] == 0
    assert meta["strata_targets"]["medium_low"] == 6
    assert meta["strata_shortfall"] == {}
    assert meta["rows_in_corpus"] == 6
    assert meta["corpus_is_partial"] is True


def test_strata_stay_balanced_on_a_partial_corpus():
    """Both strata available: still a 50/50 split of whatever size N implies."""
    rows = ([_row(f"H{i}", CATEGORIES[i % 10], "high") for i in range(20)]
            + [_row(f"L{i}", CATEGORIES[i % 10], "low") for i in range(20)])
    picked, meta = audit_mod.sample(rows, forced=[])
    n = audit_mod.derive_size(len(rows))
    assert len(picked) == n
    assert meta["strata"]["high"] == n // 2
    assert meta["strata"]["medium_low"] == n - n // 2


def test_sample_shrinks_when_corpus_is_smaller_than_requested():
    rows = ([_row(f"H{i}", CATEGORIES[i % 4], "high") for i in range(5)]
            + [_row(f"L{i}", CATEGORIES[i % 4], "medium") for i in range(5)])
    picked, meta = audit_mod.sample(rows, size=20, forced=[])
    assert len(picked) == 10
    assert meta["actual_size"] == 10
    assert meta["size_derivation"]["was_overridden"] is True
    assert meta["corpus_is_partial"] is True


def test_lenient_name_matching_finds_renamed_products():
    assert audit_mod._matches("Amazon Selling Partner API", "Amazon SP-API") is False \
        or audit_mod._matches("Amazon Selling Partner API", "Amazon API")
    assert audit_mod._matches("Salesforce Commerce Cloud", "salesforce commerce cloud")
    assert not audit_mod._matches("Salesforce", "Salesforce Commerce Cloud")


# ---------------- queue CSV ----------------


def test_queue_csv_has_the_agreed_columns_and_blank_verdicts(data_dir):
    picked, meta = audit_mod.sample(_corpus(), forced=[])
    path = audit_mod.write_queue(picked, meta)

    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    assert list(rows[0].keys()) == audit_mod.QUEUE_COLUMNS
    assert len(rows) == meta["actual_size"]
    for r in rows:
        for col in ("verdict_auth", "verdict_tier", "verdict_api",
                    "verdict_mcp", "verdict_buildable",
                    "truth_notes", "why_it_failed"):
            assert r[col] == "", f"{col} must ship blank for the auditor"
        assert r["agent_confidence"] in {"high", "medium", "low"}
        assert r["agent_auth_methods"]  # agent claims are pre-filled
    assert (data_dir / "audit_sample_meta.json").exists()


def test_list_fields_are_pipe_joined(data_dir):
    row = _row("Acme", "CRM & Sales", "high")
    row["auth_methods"] = ["OAuth2", "API Key"]
    path = audit_mod.write_queue([row], {"seed": 1})
    with path.open(encoding="utf-8-sig", newline="") as fh:
        r = next(csv.DictReader(fh))
    assert r["agent_auth_methods"] == "OAuth2 | API Key"
    assert r["agent_has_mcp"] == "false"


# ---------------- scorer ----------------


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=audit_mod.QUEUE_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in audit_mod.QUEUE_COLUMNS})


def test_precision_by_field_on_a_hand_built_fixture(tmp_path):
    csv_path = tmp_path / "q.csv"
    _write_csv(csv_path, [
        {"name": "A", "agent_confidence": "high", "agent_access_tier": "free",
         "verdict_tier": "correct", "verdict_auth": "correct"},
        {"name": "B", "agent_confidence": "high", "agent_access_tier": "free",
         "verdict_tier": "wrong", "verdict_auth": "correct",
         "truth_notes": "needs Enterprise", "why_it_failed": "pricing page not fetched"},
        {"name": "C", "agent_confidence": "low", "agent_access_tier": "free",
         "verdict_tier": "partially_correct", "verdict_auth": "correct"},
        {"name": "D", "agent_confidence": "low", "agent_access_tier": "free",
         "verdict_tier": "unverifiable", "verdict_auth": "wrong"},
    ])

    res = score_audit(csv_path)

    tier = res["precision_by_field"]["access_tier"]
    # unverifiable excluded from the denominator: 3 scored, 1 correct
    assert tier["scored"] == 3
    assert tier["correct"] == 1 and tier["wrong"] == 1
    assert tier["partially_correct"] == 1 and tier["unverifiable"] == 1
    # Reported values are rounded to 4dp, hence the explicit tolerance.
    assert tier["precision"] == pytest.approx(1 / 3, abs=1e-4)
    assert tier["precision_lenient"] == pytest.approx(1.5 / 3, abs=1e-4)

    auth = res["precision_by_field"]["auth_methods"]
    assert auth["scored"] == 4
    assert auth["precision"] == pytest.approx(3 / 4)


def test_precision_by_confidence_is_the_headline(tmp_path):
    csv_path = tmp_path / "q.csv"
    _write_csv(csv_path, [
        {"name": "A", "agent_confidence": "high", "verdict_tier": "correct"},
        {"name": "B", "agent_confidence": "high", "verdict_tier": "correct"},
        {"name": "C", "agent_confidence": "medium", "verdict_tier": "wrong"},
        {"name": "D", "agent_confidence": "low", "verdict_tier": "correct"},
    ])

    res = score_audit(csv_path)
    assert res["precision_by_confidence"]["high"]["precision"] == 1.0
    assert res["precision_by_confidence"]["medium_low"]["precision"] == 0.5
    # medium and low are pooled into one bucket, high stands alone
    assert set(res["precision_by_confidence"]) == {"high", "medium_low"}


def test_misses_capture_why_it_failed(tmp_path):
    csv_path = tmp_path / "q.csv"
    _write_csv(csv_path, [
        {"name": "B", "agent_confidence": "high", "agent_access_tier": "free",
         "verdict_tier": "wrong", "truth_notes": "needs Enterprise",
         "why_it_failed": "pricing page never fetched"},
    ])
    res = score_audit(csv_path)
    assert len(res["misses"]) == 1
    m = res["misses"][0]
    assert m["app"] == "B" and m["field"] == "access_tier"
    assert m["agent_said"] == "free"
    assert m["truth"] == "needs Enterprise"
    assert m["why_it_failed"] == "pricing page never fetched"
    assert res["unverifiable_count"] == 0


def test_unfilled_rows_are_reported_not_counted_as_correct(tmp_path):
    csv_path = tmp_path / "q.csv"
    _write_csv(csv_path, [
        {"name": "A", "agent_confidence": "high", "verdict_tier": "correct"},
        {"name": "Untouched", "agent_confidence": "high"},
    ])
    res = score_audit(csv_path)
    assert res["rows_audited"] == 1
    assert res["rows_not_yet_audited"] == ["Untouched"]


def test_invalid_verdict_values_are_flagged(tmp_path):
    csv_path = tmp_path / "q.csv"
    _write_csv(csv_path, [
        {"name": "A", "agent_confidence": "high", "verdict_tier": "probably fine"},
    ])
    res = score_audit(csv_path)
    assert res["invalid_verdict_values"][0]["value"] == "probably fine"
    assert res["precision_by_field"]["access_tier"]["scored"] == 0


def test_every_verdict_column_maps_to_a_field():
    assert set(VERDICT_FIELDS) == {
        "verdict_auth", "verdict_tier", "verdict_api",
        "verdict_mcp", "verdict_buildable",
    }


# ---------------- naming discipline ----------------


def test_progression_separates_convergence_from_accuracy(data_dir):
    from toolkit_recon import progression as prog_mod

    (data_dir / "corroboration_summary.json").write_text(json.dumps(
        {"rows_compared": 10, "fully_agreeing_rows": 8, "disputed_rows": 2}),
        encoding="utf-8")

    prog = prog_mod.build()

    # Convergence is computed...
    assert prog["convergence"]["pass1_to_pass2"]["convergence_rate"] == 0.8
    assert "NOT accuracy" in prog["convergence"]["definition"]

    # ...accuracy is not, and no key named "accuracy" holds a chain-derived value.
    assert prog["accuracy"]["precision_by_confidence"] is None
    assert prog["accuracy"]["rows_audited"] is None
    assert "ONLY accuracy figure" in prog["accuracy"]["definition"]


def test_audit_results_populate_accuracy_only(data_dir):
    from toolkit_recon import progression as prog_mod

    (data_dir / "human_audit.json").write_text(json.dumps({
        "rows_in_queue": 20, "rows_audited": 20, "unverifiable_count": 3,
        "precision_by_confidence": {
            "high": {"precision": 0.9, "scored": 10},
            "medium_low": {"precision": 0.6, "scored": 10},
        },
        "precision_by_field": {"access_tier": {"precision": 0.7, "scored": 20}},
        "misses": [],
    }), encoding="utf-8")

    prog = prog_mod.build()
    assert prog["accuracy"]["rows_audited"] == 20
    assert prog["accuracy"]["confidence_signal"]["gap"] == pytest.approx(0.3)
    assert "carries signal" in prog["accuracy"]["confidence_signal"]["interpretation"]
    # The audit must not leak into the convergence block.
    assert prog["convergence"]["pass1_to_pass2"]["convergence_rate"] is None


def test_flat_confidence_signal_is_called_out(data_dir):
    """If high-confidence rows are no better than low, the confidence column
    is decoration and the report must say so."""
    from toolkit_recon import progression as prog_mod

    (data_dir / "human_audit.json").write_text(json.dumps({
        "rows_in_queue": 20, "rows_audited": 20,
        "precision_by_confidence": {
            "high": {"precision": 0.6, "scored": 10},
            "medium_low": {"precision": 0.65, "scored": 10},
        },
    }), encoding="utf-8")

    prog = prog_mod.build()
    assert "does NOT separate" in prog["accuracy"]["confidence_signal"]["interpretation"]
