"""Unit tests for the parts that must not silently drift: the strict JSON
schema handed to the LLM, the confidence rules, and the URL ranker."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toolkit_recon.apps import APPS, BY_SLUG  # noqa: E402
from toolkit_recon.confidence import assign_confidence  # noqa: E402
from toolkit_recon.extract import strict_schema  # noqa: E402
from toolkit_recon.ranking import is_official, rank  # noqa: E402
from toolkit_recon.schema import AppResearch, Extraction, SearchHit  # noqa: E402


def _ex(**kw) -> Extraction:
    base = dict(
        one_liner="x",
        auth_methods=["OAuth2"],
        access_tier="self_serve_free",
        api_style=["REST"],
        api_breadth="moderate",
        has_mcp=False,
        mcp_evidence_url=None,
        buildable_today="yes",
        primary_blocker=None,
        evidence_urls=["https://docs.example.com/auth"],
        agent_notes="",
        signal_auth_in_official_docs=True,
        signal_tier_explicitly_stated=True,
        signal_sources_conflict=False,
    )
    base.update(kw)
    return Extraction(**base)


# ---------------- strict schema ----------------


def test_strict_schema_is_fully_closed():
    s = strict_schema(Extraction)
    assert "$defs" not in s, "refs must be inlined for strict mode"
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"]), "strict mode requires every field"

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)


def test_enums_survive_inlining():
    s = strict_schema(Extraction)
    assert "OAuth2" in s["properties"]["auth_methods"]["items"]["enum"]
    assert "partner_gated" in s["properties"]["access_tier"]["enum"]


def test_nullable_field_is_a_union():
    s = strict_schema(Extraction)
    types = {b.get("type") for b in s["properties"]["mcp_evidence_url"]["anyOf"]}
    assert types == {"string", "null"}


# ---------------- confidence rules ----------------


def test_high_needs_official_auth_and_explicit_tier():
    c, _ = assign_confidence(_ex(), official_docs_reached=True, docs_fetched=2)
    assert c == "high"


def test_inferred_tier_downgrades_to_medium():
    c, r = assign_confidence(
        _ex(signal_tier_explicitly_stated=False), official_docs_reached=True, docs_fetched=2
    )
    assert c == "medium" and "inferred" in r


def test_no_official_docs_forces_low_even_if_model_claims_otherwise():
    # The critical guard: the model says it saw official docs, but the
    # pipeline never reached an official domain. Code wins.
    c, r = assign_confidence(_ex(), official_docs_reached=False, docs_fetched=2)
    assert c == "low" and "official" in r


def test_conflicting_sources_force_low():
    c, _ = assign_confidence(
        _ex(signal_sources_conflict=True), official_docs_reached=True, docs_fetched=3
    )
    assert c == "low"


def test_zero_documents_is_low():
    c, _ = assign_confidence(_ex(), official_docs_reached=True, docs_fetched=0)
    assert c == "low"


def test_missing_auth_section_is_medium_not_high():
    c, _ = assign_confidence(
        _ex(signal_auth_in_official_docs=False), official_docs_reached=True, docs_fetched=2
    )
    assert c == "medium"


# ---------------- ranking ----------------


def test_official_docs_outrank_blogspam():
    hits = [
        SearchHit(url="https://medium.com/@someone/stripe-api-guide"),
        SearchHit(url="https://docs.stripe.com/api/authentication"),
    ]
    out = rank(hits, ("stripe.com",), 3)
    assert out[0].url.startswith("https://docs.stripe.com")
    assert "blogspam" in out[-1].reason


def test_integrator_sites_are_downranked():
    hits = [
        SearchHit(url="https://zapier.com/apps/asana/integrations"),
        SearchHit(url="https://developers.asana.com/docs/authentication"),
    ]
    out = rank(hits, ("asana.com",), 2)
    assert "asana.com" in out[0].url


def test_non_official_hosts_are_capped_at_two():
    hits = [SearchHit(url=f"https://example{i}.org/api/docs") for i in range(6)]
    out = rank(hits, ("acme.com",), 5)
    assert len(out) == 2


def test_is_official_matches_subdomains_not_suffix_lookalikes():
    assert is_official("https://developers.notion.com/reference", ("notion.com",))
    assert not is_official("https://notnotion.com/docs", ("notion.com",))


def test_dedupes_urls_and_strips_fragments():
    hits = [
        SearchHit(url="https://docs.acme.com/auth"),
        SearchHit(url="https://docs.acme.com/auth#tokens"),
        SearchHit(url="https://docs.acme.com/auth/"),
    ]
    assert len(rank(hits, ("acme.com",), 5)) == 1


# ---------------- app list & output contract ----------------


def test_app_list_is_exactly_100_unique():
    assert len(APPS) == 100
    assert len(BY_SLUG) == 100
    assert len({a.name for a in APPS}) == 100


def test_every_app_declares_official_domains():
    for a in APPS:
        assert a.official_domains, f"{a.slug} has no official domains"


def test_each_pass_asks_different_questions():
    """A re-check pass that reused pass 1's queries would hit the same cache
    and reproduce the same row, measuring nothing."""
    from toolkit_recon.pipeline import queries_for

    app = BY_SLUG["slack"]
    sets = [tuple(queries_for(app, p)) for p in (1, 2, 3)]
    assert len(set(sets)) == 3, "passes must not reuse the same queries"
    assert all(len(s) == 2 for s in sets)
    assert all("Slack" in q for s in sets for q in s)


def test_unknown_pass_falls_back_to_pass_one():
    from toolkit_recon.pipeline import queries_for

    app = BY_SLUG["slack"]
    assert queries_for(app, 99) == queries_for(app, 1)


def test_failure_row_is_schema_valid_and_flagged():
    from toolkit_recon.pipeline import failure_row

    row = failure_row(BY_SLUG["slack"], "boom", 1)
    assert row.confidence == "low"
    assert row.agent_notes.startswith("RESEARCH FAILED")
    assert len(row.evidence_urls) >= 1  # schema demands one; must not be a fake docs URL
    assert not row.evidence_urls[0].startswith("http")


def test_output_row_requires_at_least_one_evidence_url():
    import pytest

    with pytest.raises(Exception):
        AppResearch(
            name="X", category="Y", one_liner="z", auth_methods=["OAuth2"],
            access_tier="self_serve_free", api_style=["REST"], api_breadth="narrow",
            has_mcp=False, mcp_evidence_url=None, buildable_today="yes",
            primary_blocker=None, evidence_urls=[], confidence="high",
            agent_notes="", pass_number=1,
        )
