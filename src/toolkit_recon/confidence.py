"""Derive the confidence column from concrete signals.

The brief's rules, verbatim:

    high   - auth section found in official docs, tier explicitly stated
    medium - auth found, tier inferred from pricing page or absent
    low    - no official docs reached, or sources conflict

Two guards sit on top of the LLM's self-reported signals:

1. `official_docs_reached` is measured by *us* — it is true only if a fetch
   against a domain declared official for that app actually returned text. The
   model cannot talk its way into a high score on a page we never retrieved.
2. A claimed `auth_in_official_docs` is downgraded when no official page was
   actually fetched. This is where most naive pipelines quietly overrate
   themselves.

Every decision returns a human-readable reason, which lands in the trace so a
reviewer can audit why any given row is rated the way it is.
"""

from __future__ import annotations

from .schema import Confidence, Extraction


def assign_confidence(
    ex: Extraction,
    *,
    official_docs_reached: bool,
    docs_fetched: int,
) -> tuple[Confidence, str]:
    if docs_fetched == 0:
        return "low", "no documents were successfully fetched"

    if ex.signal_sources_conflict:
        return "low", "extractor reported conflicting sources"

    if not official_docs_reached:
        return "low", "no official vendor domain was reached; only third-party sources"

    auth_found = ex.signal_auth_in_official_docs
    if not auth_found:
        return (
            "medium",
            "official docs reached but no explicit auth section identified",
        )

    if ex.signal_tier_explicitly_stated:
        return "high", "auth section found in official docs and access tier explicitly stated"

    return "medium", "auth found in official docs but access tier inferred or absent"
