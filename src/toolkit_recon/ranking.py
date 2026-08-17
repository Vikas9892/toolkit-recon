"""Rank search hits so official documentation beats blogspam.

The confidence rules hinge on whether we reached *official* docs, so
"is this URL official?" is a first-class decision, not a heuristic buried in
the prompt. Each app in apps.py declares its own domains; anything on those
domains scores highest, and known content-farm hosts are pushed to the bottom.
"""

from __future__ import annotations

from .schema import SearchHit
from .throttle import domain_of

# Hosts that overwhelmingly republish or summarise vendor docs.
BLOGSPAM = {
    "medium.com", "dev.to", "hashnode.dev", "blogspot.com", "wordpress.com",
    "quora.com", "reddit.com", "pinterest.com", "slideshare.net", "scribd.com",
    "linkedin.com", "facebook.com", "x.com", "twitter.com", "youtube.com",
    "geeksforgeeks.org", "tutorialspoint.com", "javatpoint.com", "w3schools.com",
    "toolify.ai", "futurepedia.io", "producthunt.com", "g2.com", "capterra.com",
    "getapp.com", "trustradius.com", "softwareadvice.com", "sourceforge.net",
    "zapier.com", "make.com", "n8n.io",  # integrators, not the vendor
}

# Generic developer-portal hosts: not vendor-owned, but far better than a blog.
NEUTRAL_GOOD = {
    "github.com", "stackoverflow.com", "postman.com", "rapidapi.com",
    "readthedocs.io", "npmjs.com", "pypi.org", "openapis.org",
}

# Path fragments that signal we landed on the page we actually wanted.
PATH_BONUSES: list[tuple[tuple[str, ...], float, str]] = [
    (("/auth", "authentication", "authorization", "oauth", "/token"), 3.0, "auth-path"),
    (("developer", "/docs", "/api", "/reference", "/rest"), 2.0, "docs-path"),
    (("pricing", "/plans", "billing"), 1.5, "pricing-path"),
    (("mcp",), 2.5, "mcp-path"),
    (("graphql",), 1.0, "graphql-path"),
]

PATH_PENALTIES: list[tuple[tuple[str, ...], float, str]] = [
    (("/blog", "/news", "/press", "/careers", "/jobs"), -2.5, "blog-path"),
    (("/community", "/forum", "/discuss"), -1.5, "forum-path"),
    (("/status",), -2.0, "status-path"),
]


def is_official(url: str, official_domains: tuple[str, ...]) -> bool:
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in official_domains)


def score_hit(hit: SearchHit, official_domains: tuple[str, ...]) -> SearchHit:
    host = domain_of(hit.url)
    score = 0.0
    reasons: list[str] = []

    if is_official(hit.url, official_domains):
        score += 10.0
        reasons.append("official-domain")
        # docs.* / developer.* subdomains are the canonical reference.
        if host.split(".")[0] in {"docs", "developer", "developers", "api", "dev"}:
            score += 3.0
            reasons.append("docs-subdomain")
    elif any(host == b or host.endswith("." + b) for b in BLOGSPAM):
        score -= 6.0
        reasons.append("blogspam")
    elif any(host == n or host.endswith("." + n) for n in NEUTRAL_GOOD):
        score += 1.0
        reasons.append("neutral-dev-host")

    low = hit.url.lower()
    for frags, pts, label in PATH_BONUSES:
        if any(f in low for f in frags):
            score += pts
            reasons.append(label)
            break
    for frags, pts, label in PATH_PENALTIES:
        if any(f in low for f in frags):
            score += pts
            reasons.append(label)
            break

    # Prefer canonical short URLs over deep-linked anchors of the same content.
    score -= 0.15 * low.count("/")

    hit.score = round(score, 2)
    hit.reason = ",".join(reasons) or "none"
    return hit


def rank(
    hits: list[SearchHit], official_domains: tuple[str, ...], limit: int
) -> list[SearchHit]:
    """Score, dedupe by URL, and cap at `limit`.

    Also caps non-official hosts at two so a well-SEO'd third party cannot
    crowd out the vendor's own docs.
    """
    seen: set[str] = set()
    scored: list[SearchHit] = []
    for h in hits:
        norm = h.url.rstrip("/").split("#")[0]
        if norm in seen:
            continue
        seen.add(norm)
        h.url = norm
        scored.append(score_hit(h, official_domains))

    scored.sort(key=lambda h: h.score, reverse=True)

    out: list[SearchHit] = []
    non_official = 0
    for h in scored:
        if not is_official(h.url, official_domains):
            if non_official >= 2:
                continue
            non_official += 1
        out.append(h)
        if len(out) >= limit:
            break
    return out
