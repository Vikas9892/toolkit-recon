"""Reduce a fetched page to the parts that answer the schema.

Blind truncation is the obvious way to fit a token budget and the worst one:
auth details usually sit halfway down a docs page, so `text[:3000]` reliably
throws away the exact evidence the confidence rules depend on.

Instead we keep the opening (product identity, needed for `one_liner`) and
then the highest-scoring blocks, restored to document order so the model reads
coherent prose rather than a bag of fragments. Dropped regions are marked so
the model can tell an omission from an absence.
"""

from __future__ import annotations

import re

# Weighted by how directly a term settles a schema field.
KEYWORDS: dict[str, float] = {
    # auth_methods — the highest-value evidence
    "authentication": 6.0, "authorization": 5.0, "oauth": 6.0, "oauth2": 6.0,
    "api key": 6.0, "apikey": 5.0, "bearer": 5.0, "access token": 4.5,
    "personal access token": 4.5, "basic auth": 4.5, "client_secret": 4.0,
    "client id": 3.0, "scope": 3.0, "refresh token": 3.0, "jwt": 2.5,
    "service account": 3.0, "signature": 1.5,
    # access_tier
    "pricing": 4.0, "plan": 2.5, "enterprise": 3.0, "free tier": 4.0,
    "trial": 3.0, "subscription": 2.0, "paid": 2.5, "quota": 2.0,
    "admin": 3.0, "partner": 4.0, "approval": 3.5, "request access": 4.5,
    "apply for": 3.5, "allowlist": 3.0, "sandbox": 2.0,
    # api_style / breadth
    "rest api": 4.0, "graphql": 4.5, "webhook": 3.0, "endpoint": 2.5,
    "sdk": 2.0, "openapi": 3.0, "rate limit": 2.5, "api reference": 3.0,
    "deprecated": 2.0, "versioning": 1.5,
    # mcp
    "model context protocol": 8.0, "mcp server": 8.0, "mcp": 4.0,
}

_KEY_RE = {k: re.compile(re.escape(k), re.I) for k in KEYWORDS}
_HEADING = re.compile(r"^\s{0,3}#{1,4}\s+\S")


def _blocks(text: str) -> list[str]:
    parts = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    # Very long blocks (tables, code dumps) get split so one block cannot eat
    # the whole budget.
    out: list[str] = []
    for b in parts:
        while len(b) > 1200:
            cut = b.rfind("\n", 0, 1200)
            cut = cut if cut > 400 else 1200
            out.append(b[:cut].strip())
            b = b[cut:].strip()
        if b:
            out.append(b)
    return out


def score_block(block: str) -> float:
    low = block.lower()
    score = sum(w * min(2, len(rx.findall(low))) for k, (rx, w) in
                ((k, (_KEY_RE[k], KEYWORDS[k])) for k in KEYWORDS)
                if rx.search(low))
    if _HEADING.match(block):
        score += 1.5  # headings orient the model cheaply
    if len(block) < 40:
        score -= 1.0
    return score


def condense(text: str, budget: int, lead: int = 900) -> str:
    """Return at most ~`budget` characters of the most relevant content.

    The lead is capped at a third of the budget: on a tight per-document
    allowance an uncapped lead would consume everything and leave no room for
    the scored blocks, which is precisely the failure this module prevents.
    """
    text = text.strip()
    if len(text) <= budget:
        return text

    lead = max(120, min(lead, budget // 3))
    head = text[:lead]
    rest = text[lead:]

    blocks = _blocks(rest)
    scored = sorted(
        ((score_block(b), i, b) for i, b in enumerate(blocks)),
        key=lambda t: t[0],
        reverse=True,
    )

    remaining = budget - len(head)
    chosen: list[tuple[int, str]] = []
    for sc, i, b in scored:
        if sc <= 0:
            break
        if len(b) + 8 > remaining:
            continue
        chosen.append((i, b))
        remaining -= len(b) + 8
        if remaining < 120:
            break

    chosen.sort(key=lambda t: t[0])

    out = [head]
    prev = -1
    for i, b in chosen:
        if i != prev + 1:
            out.append("\n\n[... omitted ...]\n\n")
        out.append(b)
        prev = i
    return "".join(out)[:budget]
