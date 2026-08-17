"""Schema-constrained extraction.

The model is handed the fetched documents and must reply with JSON that
validates against `Extraction`. There is no free text for the code to parse:
the JSON schema is enforced at the API boundary via `response_format`, and
then re-validated locally by Pydantic. If either fails, that is an error the
caller handles — not something we regex our way out of.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .condense import condense
from .config import settings
from .ratelimit import TokenRateLimiter, estimate_tokens
from .schema import Extraction
from .throttle import PermanentError, RetryableError, with_backoff

SYSTEM_PASS1 = """\
You are a research analyst profiling SaaS products for an agent-tooling team.
The team builds toolkits that let AI agents call these products' APIs, so the
question behind every field is: could an engineer ship a working toolkit for
this product, and what would stop them?

Rules:
- Use ONLY the supplied documents. Do not use prior knowledge to fill gaps.
- If the documents do not establish something, say so via the signal flags and
  the notes field rather than guessing.
- evidence_urls must be chosen from the SOURCE URLs listed with the documents.
- has_mcp is true ONLY with an explicit reference to a Model Context Protocol
  server for this product. Absence of evidence is not evidence; default false.
- primary_blocker is the single biggest obstacle to shipping a toolkit today
  (for example: "API access requires an Enterprise plan", "partner approval
  needed for write scopes"). Null when nothing meaningful blocks it.
- The three signal_* booleans are audited against what was actually fetched.
  Report them honestly; overclaiming is worse than a low confidence score.

Field guidance:
- access_tier: no_public_api when there is no documented public API at all;
  partner_gated when access needs a commercial/partner agreement;
  admin_approval when a workspace admin must enable it; paid_plan_required
  when a paid tier is required; self_serve_trial / self_serve_free otherwise.
- api_breadth: narrow = a handful of endpoints or one domain object;
  moderate = several resource families; broad = most of the product surface.
- buildable_today: "yes" for self-serve auth and a documented public API;
  "yes_with_caveats" when it works but with a real constraint (paid tier,
  admin enablement, narrow surface); "no" when there is no usable public API
  or access is effectively closed.
"""

# Pass 2 deliberately changes the *lens*, not just the wording. Pass 1 asks
# "what does this product offer?"; pass 2 asks "what will break when I try to
# ship it?". Two passes that agree despite reasoning from opposite priors are
# genuine corroboration. Re-running the same framing with new search terms
# would mostly re-measure the same bias.
SYSTEM_PASS2 = """\
You are a senior integration engineer doing a build-readiness review. Your team
has been asked to ship an agent toolkit for this product next sprint, and your
job is to find the reasons that will fail — before the sprint starts, not after.

Work adversarially. For every claim the documents appear to support, ask what
the documents do NOT say. Vendors describe the happy path; you are looking for
the gate behind it.

Rules:
- Use ONLY the supplied documents. Do not use prior knowledge to fill gaps.
- Marketing language is not evidence. "Powerful API" means nothing; a
  documented endpoint with a documented auth scheme means something.
- Where the documents are silent, prefer the more restrictive reading and say
  so in the notes. Silence about a free tier is not evidence of a free tier.
- evidence_urls must be chosen from the SOURCE URLs listed with the documents.
- has_mcp is true ONLY with an explicit reference to a Model Context Protocol
  server for this product. Default false.
- primary_blocker is the thing most likely to stop the sprint. Null only when
  you genuinely cannot find one.
- The three signal_* booleans are audited against what was actually fetched.
  Report them honestly; overclaiming is worse than a low confidence score.

Field guidance:
- access_tier: pick the tier a new developer would actually hit, not the best
  case. no_public_api when there is no documented public API; partner_gated
  when a commercial/partner agreement is needed; admin_approval when a
  workspace admin must enable it; paid_plan_required when a paid tier is
  required; self_serve_trial / self_serve_free only when genuinely open.
- api_breadth: narrow = a handful of endpoints or one domain object;
  moderate = several resource families; broad = most of the product surface.
  Judge by what is documented, not by how large the product is.
- buildable_today: "yes" only when auth is self-serve AND the API is publicly
  documented AND nothing gates it. "yes_with_caveats" when a real constraint
  exists. "no" when there is no usable public API or access is effectively
  closed.
"""

SYSTEM_BY_PASS: dict[int, str] = {1: SYSTEM_PASS1, 2: SYSTEM_PASS2, 3: SYSTEM_PASS2}


def system_for(pass_number: int) -> str:
    return SYSTEM_BY_PASS.get(pass_number, SYSTEM_PASS1)


def strict_schema(model: type[Extraction]) -> dict[str, Any]:
    """Turn a Pydantic model into a strict JSON schema the API will enforce.

    Strict mode requires every property listed in `required` and
    `additionalProperties: false` on every object. Optionality is expressed as
    a nullable union, which is why the model uses `str | None` rather than
    fields with defaults.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                merged = {**inline(defs[name])}
                merged.update({k: v for k, v in node.items() if k != "$ref"})
                return merged
            out = {k: inline(v) for k, v in node.items()}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(schema)


def build_user_prompt(name: str, category: str, docs: list[tuple[str, str]], hint: str) -> str:
    """Assemble the prompt, condensing documents to fit the per-call budget.

    The budget is split evenly across documents, then each document is
    condensed by relevance rather than truncated, so a page that buries its
    auth section on screen three still contributes that section.
    """
    parts = [f"PRODUCT: {name}\nCATEGORY: {category}\n"]
    if hint:
        parts.append(
            "SEARCH SUMMARY (context only, not citable as evidence):\n"
            f"{hint[: settings.hint_chars]}\n"
        )
    parts.append("DOCUMENTS:\n")

    per_doc = max(800, settings.prompt_doc_budget // max(1, len(docs)))
    for i, (url, text) in enumerate(docs, 1):
        body = condense(text, per_doc)
        note = "" if len(body) >= len(text) else " (condensed to the sections most relevant to the schema)"
        parts.append(f"--- DOCUMENT {i}{note} ---\nSOURCE URL: {url}\n\n{body}\n")

    parts.append(
        "\nProfile this product against the schema. Cite only SOURCE URLs listed above."
    )
    return "\n".join(parts)


class Extractor:
    def __init__(self, pass_number: int = 1) -> None:
        if not settings.llm_api_key:
            raise RuntimeError(
                "No LLM key. Set GROQ_API_KEY (or LLM_API_KEY) in the environment."
            )
        self.pass_number = pass_number
        self.system = system_for(pass_number)
        self._schema = strict_schema(Extraction)
        self.limiter = TokenRateLimiter(
            tokens_per_minute=settings.llm_tokens_per_minute
        )
        # The endpoint's quota, not our worker count, is the real constraint on
        # extraction — so the LLM gets its own narrower gate.
        self._gate = asyncio.Semaphore(settings.llm_concurrency)
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=settings.request_timeout,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def extract(
        self, name: str, category: str, docs: list[tuple[str, str]], hint: str = ""
    ) -> tuple[Extraction, dict[str, int]]:
        prompt = build_user_prompt(name, category, docs, hint)
        body = {
            "model": settings.llm_model,
            "temperature": 0,
            "max_completion_tokens": settings.llm_max_completion_tokens,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "app_research_extraction",
                    "strict": True,
                    "schema": self._schema,
                },
            },
        }

        # Reserve against the *expected* completion size, not the safety cap;
        # `settle` corrects either way once the API reports real usage.
        estimated = (
            estimate_tokens(self.system)
            + estimate_tokens(prompt)
            + settings.llm_expected_completion_tokens
        )

        async def _attempt() -> httpx.Response:
            # Pace before spending: a 429 costs quota too, so backing off after
            # the fact is strictly worse than not overspending in the first place.
            await self.limiter.acquire(estimated)
            async with self._gate:
                try:
                    r = await self._client.post("/chat/completions", json=body)
                except httpx.HTTPError as e:
                    raise RetryableError(str(e)) from e
            if r.status_code == 429:
                # Trust the server's own guidance over our backoff curve.
                retry_after = r.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 20.0
                except ValueError:
                    delay = 20.0
                self.limiter.penalise(min(delay + 1.0, 90.0))
                raise RetryableError(f"429: {r.text[:160]}")
            if r.status_code >= 500:
                raise RetryableError(f"{r.status_code}: {r.text[:200]}")
            if r.status_code >= 400:
                # Constrained decoding occasionally fails to close the object.
                # That is a sampling accident, not a bad request — the same
                # prompt usually succeeds on the next attempt, so retry it.
                low = r.text.lower()
                if any(
                    s in low
                    for s in ("json_validate_failed", "failed to generate json",
                              "failed to validate json")
                ):
                    raise RetryableError(f"{r.status_code} (json decode): {r.text[:160]}")
                raise PermanentError(f"{r.status_code}: {r.text[:400]}")

            # A truncated completion yields invalid JSON downstream; catch it
            # here where we can still retry with a clean slate.
            if (r.json().get("choices") or [{}])[0].get("finish_reason") == "length":
                raise RetryableError("completion truncated (finish_reason=length)")
            return r

        resp = await with_backoff(_attempt, attempts=settings.llm_retries)
        payload = resp.json()
        usage = payload.get("usage") or {}
        content = payload["choices"][0]["message"]["content"]

        # Reconcile our estimate against what was actually billed, so a run of
        # under-estimates cannot drift the window into 429 territory.
        self.limiter.settle(estimated, int(usage.get("total_tokens") or 0))

        # Enforced at the API boundary, re-validated here. Belt and braces:
        # a schema violation is a hard error, never a silent partial row.
        extraction = Extraction.model_validate(json.loads(content))
        tokens = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        return extraction, tokens
