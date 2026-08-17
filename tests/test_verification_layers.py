"""Tests for the three verification layers.

The property under test throughout: no layer may accept a trust signal the
model supplied. Layer 1 reads only disk artifacts, Layer 2 earns promotion
from measured inter-pass agreement, Layer 3 discards any verdict whose quote
is not literally present in the page we captured.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from toolkit_recon.browser_verify import quote_is_grounded  # noqa: E402
from toolkit_recon.corroborate import compare, _up  # noqa: E402
# Sampling and scoring moved to toolkit_recon.audit / report.score_audit;
# see tests/test_audit_harness.py.
from toolkit_recon.validate import (  # noqa: E402
    Context, RULE_BY_ID, apply_actions, check_row,
)


def _row(**kw) -> dict:
    base = dict(
        name="Acme", category="CRM", one_liner="x",
        auth_methods=["OAuth2"], access_tier="self_serve_free",
        api_style=["REST"], api_breadth="moderate", has_mcp=False,
        mcp_evidence_url=None, buildable_today="yes", primary_blocker=None,
        evidence_urls=["https://docs.acme.com/auth"], confidence="high",
        agent_notes="", pass_number=1,
    )
    base.update(kw)
    return base


def _ctx(**kw) -> Context:
    c = Context("acme", "Acme", ("acme.com",))
    c.fetched_urls = {"https://docs.acme.com/auth"}
    c.doc_chars = {"https://docs.acme.com/auth": 4000}
    c.doc_text = {"https://docs.acme.com/auth": "OAuth2 and API keys."}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _ids(vs) -> set[str]:
    return {v.rule for v in vs}


# ---------------- Layer 1 ----------------


def test_clean_row_produces_no_violations():
    assert check_row(_row(), _ctx()) == []


def test_r1_empty_evidence_forces_low():
    # evidence_urls=[] cannot be constructed through AppResearch, but a
    # hand-edited or externally-supplied file can contain it.
    vs = check_row(_row(evidence_urls=[]), _ctx())
    assert "R1_EVIDENCE_EMPTY" in _ids(vs)
    assert apply_actions(_row(evidence_urls=[]), vs)["confidence"] == "low"


def test_r2_catches_citation_we_never_fetched():
    vs = check_row(_row(evidence_urls=["https://docs.acme.com/invented"]), _ctx())
    assert "R2_EVIDENCE_NOT_FETCHED" in _ids(vs)


def test_r2_skipped_for_failure_rows():
    row = _row(evidence_urls=["unresolved://acme"],
               agent_notes="RESEARCH FAILED: boom")
    assert "R2_EVIDENCE_NOT_FETCHED" not in _ids(check_row(row, _ctx()))


def test_r3_rejects_mcp_claim_when_page_never_mentions_mcp():
    row = _row(has_mcp=True, mcp_evidence_url="https://docs.acme.com/auth")
    assert "R3_MCP_UNVERIFIED" in _ids(check_row(row, _ctx()))


def test_r3_rejects_mcp_evidence_from_a_third_party_directory():
    """Braze's MCP claim rested on apis.io, an API directory. A directory
    listing is not the vendor saying so, and directories go stale."""
    url = "https://apis.io/providers/acme"
    ctx = _ctx(fetched_urls={url},
               doc_chars={url: 4000},
               doc_text={url: "Acme MCP server is available here."})
    row = _row(has_mcp=True, mcp_evidence_url=url, evidence_urls=[url])
    vs = check_row(row, ctx)
    assert "R3_MCP_UNVERIFIED" in _ids(vs)
    assert any("not on a vendor domain" in v.detail for v in vs)


def test_r3_accepts_mcp_claim_backed_by_the_page():
    ctx = _ctx(doc_text={"https://docs.acme.com/auth": "Our MCP server is live."})
    row = _row(has_mcp=True, mcp_evidence_url="https://docs.acme.com/auth")
    assert "R3_MCP_UNVERIFIED" not in _ids(check_row(row, ctx))


def test_r4_flags_buildable_yes_against_gated_tier():
    for tier in ("partner_gated", "no_public_api"):
        vs = check_row(_row(buildable_today="yes", access_tier=tier), _ctx())
        assert "R4_CONTRADICTION_TIER" in _ids(vs), tier


def test_r5_flags_no_api_style_with_buildable_yes():
    vs = check_row(_row(api_style=["None"], buildable_today="yes"), _ctx())
    assert "R5_CONTRADICTION_STYLE" in _ids(vs)


def test_r6_unknown_auth_forces_low():
    row = _row(auth_methods=["Unknown"], confidence="high")
    vs = check_row(row, _ctx())
    assert "R6_AUTH_UNKNOWN" in _ids(vs)
    assert apply_actions(row, vs)["confidence"] == "low"


def test_r7_downgrades_one_level_when_nothing_official():
    row = _row(evidence_urls=["https://randomblog.example/acme"], confidence="high")
    ctx = _ctx(fetched_urls={"https://randomblog.example/acme"})
    vs = check_row(row, ctx)
    assert "R7_NON_OFFICIAL_EVIDENCE" in _ids(vs)
    assert apply_actions(row, vs)["confidence"] == "medium"  # one level, not to low


def test_r8_flags_when_every_archived_doc_is_thin():
    ctx = _ctx(doc_chars={"https://docs.acme.com/auth": 90})
    assert "R8_THIN_EVIDENCE" in _ids(check_row(_row(), ctx))


def test_r8_quiet_when_one_substantial_doc_exists():
    ctx = _ctx(doc_chars={"a": 90, "b": 5000})
    assert "R8_THIN_EVIDENCE" not in _ids(check_row(_row(), ctx))


def test_actions_only_ever_downgrade():
    """A validation rule must never be able to raise confidence."""
    order = {"low": 0, "medium": 1, "high": 2}
    for rule in RULE_BY_ID.values():
        assert rule.action in {"flag", "force_low", "downgrade"}
    row = _row(confidence="low", auth_methods=["Unknown"])
    out = apply_actions(row, check_row(row, _ctx()))
    assert order[out["confidence"]] <= order[row["confidence"]]


# ---------------- Layer 2 ----------------


def test_compare_ignores_list_ordering():
    a = _row(auth_methods=["OAuth2", "API Key"])
    b = _row(auth_methods=["API Key", "OAuth2"])
    agree, disputes = compare(a, b)
    assert disputes == []
    assert "auth_methods" in agree


def test_compare_reports_the_disagreeing_field():
    a = _row(access_tier="self_serve_free")
    b = _row(access_tier="paid_plan_required")
    _, disputes = compare(a, b)
    assert len(disputes) == 1
    assert disputes[0]["field"] == "access_tier"
    assert disputes[0]["pass1"] == "self_serve_free"
    assert disputes[0]["pass2"] == "paid_plan_required"


def test_prose_fields_are_not_compared():
    a = _row(one_liner="A CRM.", agent_notes="notes A")
    b = _row(one_liner="Totally different wording.", agent_notes="notes B")
    _, disputes = compare(a, b)
    assert disputes == []


def test_promotion_never_exceeds_high():
    assert _up("high") == "high"
    assert _up("medium") == "high"
    assert _up("low") == "medium"


# ---------------- Layer 3 ----------------


PAGE = (
    "Authentication\n\nAll requests require an OAuth2 access token.\n"
    "Send it in the Authorization header as a Bearer token.\n"
)


def test_grounded_quote_is_accepted():
    assert quote_is_grounded("All requests require an OAuth2 access token.", PAGE)


def test_quote_survives_reflowed_whitespace():
    assert quote_is_grounded("All  requests\nrequire an OAuth2   access token.", PAGE)


def test_invented_quote_is_rejected():
    # The core Layer 3 guard: a plausible-sounding fabrication must fail closed.
    assert not quote_is_grounded(
        "API access requires the Enterprise plan and partner approval.", PAGE
    )


def test_empty_or_trivial_quote_is_rejected():
    assert not quote_is_grounded("", PAGE)
    assert not quote_is_grounded("OAuth2", PAGE)  # too short to be evidence


def test_browser_floor_is_stricter_than_the_fetch_floor():
    """A live page that renders yields thousands of chars. Notion served the
    headless browser a ~2k llms.txt stub instead of its reference docs;
    judging a dispute against that would invent a resolution."""
    from toolkit_recon.config import settings

    assert settings.min_browser_chars > settings.min_doc_chars
    assert settings.min_browser_chars >= 2000


def test_paraphrase_is_rejected():
    assert not quote_is_grounded(
        "Every request needs an OAuth2 token in the header.", PAGE
    )


# ---------------- progression ----------------


def test_progression_ships_with_null_accuracy():
    """Accuracy must never be inferred from the pipeline's own agreement."""
    from toolkit_recon.progression import build

    prog = build()
    acc = prog["accuracy"]
    assert acc["precision_by_confidence"] is None
    assert acc["precision_by_field"] is None
    assert acc["rows_audited"] is None
    # No pass block may carry an "accuracy" key — that word is reserved for
    # the human-audited number and must live in exactly one place.
    for k in ("pass_1", "pass_2", "pass_3"):
        assert "accuracy" not in prog[k]
        assert "correct" not in prog[k]
