"""The contract. Everything downstream is validated against these models.

Two model families live here and the split is deliberate:

* ``AppResearch`` is the *output* contract — the row that lands in pass1.json.
* ``Extraction`` is the *LLM* contract — what the model is allowed to say.

The LLM never sets ``confidence``, ``pass_number`` or the final ``evidence_urls``.
Those are derived by code from things the code actually observed (which domains
were really reached, which fetches really succeeded). An LLM asked to rate its
own confidence rates vibes; an LLM asked "did you see an auth section in
official docs?" reports a fact we can then score. See ``confidence.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

AuthMethod = Literal[
    "OAuth2", "API Key", "Basic", "Bearer Token", "Partner/Custom", "Unknown"
]
AccessTier = Literal[
    "self_serve_free",
    "self_serve_trial",
    "paid_plan_required",
    "admin_approval",
    "partner_gated",
    "no_public_api",
]
ApiStyle = Literal["REST", "GraphQL", "SDK only", "Webhooks only", "None"]
ApiBreadth = Literal["narrow", "moderate", "broad"]
Buildable = Literal["yes", "yes_with_caveats", "no"]
Confidence = Literal["high", "medium", "low"]


class AppResearch(BaseModel):
    """One profiled SaaS app. This is the deliverable row."""

    name: str
    category: str
    one_liner: str
    auth_methods: list[AuthMethod]
    access_tier: AccessTier
    api_style: list[ApiStyle]
    api_breadth: ApiBreadth
    has_mcp: bool
    mcp_evidence_url: str | None
    buildable_today: Buildable
    primary_blocker: str | None
    evidence_urls: list[str] = Field(min_length=1)
    confidence: Confidence
    agent_notes: str
    pass_number: int

    @field_validator("auth_methods", "api_style", mode="after")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        # An empty list is not a finding, it is a missing finding. Force the
        # explicit "we don't know" / "there is none" member instead.
        return v or ["Unknown"] if v is not None else v

    @field_validator("evidence_urls", mode="after")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for u in v:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out


# --------------------------------------------------------------------------
# LLM-facing contract
# --------------------------------------------------------------------------


class Extraction(BaseModel):
    """What the LLM is permitted to return.

    Note the three ``signal_*`` booleans at the bottom: these are the concrete
    observations that ``confidence.py`` scores. Asking for observations rather
    than a self-rating is what makes the confidence column defensible.
    """

    one_liner: str = Field(description="What the product does, one line.")
    auth_methods: list[AuthMethod]
    access_tier: AccessTier
    api_style: list[ApiStyle]
    api_breadth: ApiBreadth
    has_mcp: bool
    mcp_evidence_url: str | None = Field(
        description="URL proving an official MCP server exists, else null."
    )
    buildable_today: Buildable
    primary_blocker: str | None = Field(
        description="The single biggest obstacle to shipping a toolkit, else null."
    )
    evidence_urls: list[str] = Field(
        description="URLs from the supplied documents that support these claims."
    )
    agent_notes: str = Field(description="What was ambiguous or had to be inferred.")

    signal_auth_in_official_docs: bool = Field(
        description=(
            "True only if an authentication/authorization section was found in the "
            "vendor's own documentation (not a blog, tutorial, or third-party page)."
        )
    )
    signal_tier_explicitly_stated: bool = Field(
        description=(
            "True only if the sources explicitly state which plan/tier grants API "
            "access. False if the tier was inferred from a pricing page or absent."
        )
    )
    signal_sources_conflict: bool = Field(
        description="True if the supplied sources disagree with each other."
    )


# --------------------------------------------------------------------------
# Supporting records (search hits, fetched docs, per-app trace)
# --------------------------------------------------------------------------


class SearchHit(BaseModel):
    url: str
    title: str = ""
    query: str = ""
    score: float = 0.0
    reason: str = ""


class FetchedDoc(BaseModel):
    url: str
    text: str
    ok: bool
    from_cache: bool = False
    error: str | None = None
    is_official: bool = False


class AppTrace(BaseModel):
    """One JSONL line per app in logs/trace.jsonl."""

    slug: str
    name: str
    pass_number: int
    queries: list[str] = []
    urls_ranked: list[SearchHit] = []
    urls_fetched: list[str] = []
    urls_failed: list[str] = []
    cache_hits: int = 0
    cache_misses: int = 0
    official_domains_reached: list[str] = []
    llm_model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    wall_time_s: float = 0.0
    final_confidence: str = ""
    confidence_reason: str = ""
    status: Literal["ok", "failed"] = "ok"
    error: str | None = None
    composio_request_ids: list[str] = []
