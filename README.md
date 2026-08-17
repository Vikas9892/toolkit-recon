# toolkit-recon

A research agent that profiles 100 SaaS applications for **agent-toolkit
buildability** — could an engineer ship a toolkit that lets an AI agent call
this product's API today, and if not, what stops them?

Built for an AI Product Ops take-home at Composio. The brief's constraint was
*accuracy matters more than coverage*, so the design decisions below all trade
throughput and breadth for auditability.

---

## What it produces

| Path | Contents |
|---|---|
| `data/pass1.json` | 100 rows, one per app, validated against `AppResearch` |
| `data/raw/{slug}/` | Every fetched page as text, plus a `manifest.json` |
| `logs/trace.jsonl` | One execution trace per app: queries, URLs, tokens, wall time, confidence |
| `data/checkpoints/` | Rewritten after every app, so a crash never costs the run |

---

## Quick start

```bash
pip install -r requirements.txt
pip install -e .

cp .env.example .env      # add COMPOSIO_API_KEY and GROQ_API_KEY

python -m toolkit_recon --pass-number 1          # all 100 apps
python -m toolkit_recon --only slack,stripe      # a couple, for a smoke test
python -m toolkit_recon --resume                 # continue after an interrupt
pytest -q                                        # 31 unit tests, no network
```

---

## Schema first

The contract was written before anything was scraped. `src/toolkit_recon/schema.py`
holds two model families, and the split between them is the point:

* **`AppResearch`** — the output row. What lands in `pass1.json`.
* **`Extraction`** — what the LLM is *allowed* to say.

The LLM never sets `confidence`, `pass_number`, or the final `evidence_urls`.
Those are derived by code from things the code actually observed. An LLM asked
to rate its own confidence rates vibes; an LLM asked *"did you see an auth
section in official docs?"* reports a fact that can then be scored — and
cross-checked.

---

## The confidence column

`confidence` is load-bearing: it decides which rows a human reviews, and it is
the whole verification story. The brief's rules are implemented literally in
`confidence.py`:

```
high   — auth section found in official docs, tier explicitly stated
medium — auth found, tier inferred from pricing page or absent
low    — no official docs reached, or sources conflict
```

Two guards sit on top of the model's self-reported signals:

1. **`official_docs_reached` is measured, not claimed.** It is true only if a
   fetch against a domain declared official for that app (see `apps.py`)
   actually returned text. The model cannot talk its way into a high score on
   a page the pipeline never retrieved.
2. **`evidence_urls` are intersected with what was really fetched.** A cited
   URL outside that set is dropped and the substitution is noted in
   `agent_notes`. Same for `mcp_evidence_url` — an unverifiable MCP citation
   is discarded and `has_mcp` falls back to `false`.

Every decision carries a human-readable reason into `trace.jsonl`
(`confidence_reason`), so any row can be audited without re-running anything.

---

## Pipeline

```
search (2 queries)  ->  rank  ->  fetch top 2-3  ->  extract  ->  score  ->  persist
```

1. **Search** — `{app} API documentation authentication` and
   `{app} API pricing developer access`.
2. **Rank** (`ranking.py`) — each app declares its official domains. Vendor
   docs score +10, `docs.*`/`developer.*` subdomains another +3; content farms
   and integrator sites (Medium, G2, Zapier…) are pushed down. Non-official
   hosts are capped at two per app so a well-SEO'd third party cannot crowd
   out the vendor's own documentation.
3. **Fetch** — top 2–3 URLs, cached to disk.
4. **Extract** — schema-constrained structured output. The JSON schema is
   enforced at the API boundary via `response_format`, then re-validated
   locally by Pydantic. There is no free text for the code to parse.
5. **Score** — confidence derived in code, as above.
6. **Persist** — row + raw evidence + trace + checkpoint.

### Composio in the search/fetch layer

Both network primitives run through Composio:

| Job | Tool |
|---|---|
| Search | `COMPOSIO_SEARCH_WEB` |
| Page text | `COMPOSIO_SEARCH_FETCH_URL_CONTENT` |

Both are Composio-managed (`no_auth: true`), so the project key is the only
credential and no per-app OAuth is involved.

Tool slugs and toolkit versions are **discovered at runtime**
(`get_raw_composio_tool_by_slug`) and then **pinned for the whole run**, which
the CLI prints on startup:

```
composio tool versions pinned: {"COMPOSIO_SEARCH_WEB": "20260618_00", ...}
```

Pinning matters for a research pipeline: a toolkit release mid-run would
otherwise silently change the extraction inputs and make two rows in the same
file incomparable. `COMPOSIO_SEARCH_FETCH_URL_CONTENT` also returns a
`requestId`, which is recorded per app in the trace for support-side lookup.

A keyless `DirectProvider` (DuckDuckGo + httpx + BeautifulSoup) is kept as a
fallback so the pipeline degrades instead of dying without a key. Select it
explicitly with `--provider direct`.

---

## Robustness

* **Per-app isolation.** Nothing inside `profile_app` can take down the other
  99. Any failure produces a schema-valid row with `confidence: "low"` and
  `agent_notes` starting `RESEARCH FAILED:`, and the run continues. The full
  stack trace goes to `data/raw/{slug}/_error.log` — named `.log`, not `.txt`,
  so an auditor globbing the folder for evidence does not pick up stack traces.
* **Retry policy.** Exponential backoff with full jitter on 429 and 5xx; 404
  is never retried. Permanent fetch failures are cached so a re-run does not
  re-hammer a dead URL.
* **Caching is mandatory.** Every search and every fetch is keyed by a SHA-256
  of the request and written atomically (temp file + rename), so an interrupt
  cannot leave a half-written entry. Re-runs are near-instant and free.
* **Politeness.** A global `Semaphore(8)` caps concurrency, and a per-domain
  throttle guarantees a minimum gap between two hits on the same host — eight
  workers can still all land on `docs.stripe.com` at once, and this serialises
  that without serialising the run.
* **Checkpoint after every app**, written atomically. A crash at app 87 costs
  one app. `--resume` picks up where it stopped.

### Two bugs worth calling out

Both were found by running the thing, and both are now covered by regression
tests.

**The token governor starved a worker for 68 minutes.** The extraction
endpoint enforces 8,000 tokens/minute, which is the pipeline's real
bottleneck. The first limiter released its lock while sleeping, so every
blocked worker woke on the same window roll, raced for one slot, and the
losers went back to sleep for another full window. In a five-app smoke test
one app waited 4,063 seconds while doing 3,216 tokens of actual work.
Admission is now strictly FIFO — the lock is held *across* the wait — so N
workers finish in about N windows. See `test_no_starvation_under_contention`.

**Blind truncation threw away the evidence.** Fitting a token budget by
slicing `text[:n]` reliably discards auth sections, which sit halfway down a
docs page — exactly the evidence the confidence rules depend on. `condense.py`
instead scores paragraphs against schema-relevant keywords and reassembles the
winners *in document order*, marking omissions so the model can distinguish a
gap from an absence. See `test_condense_beats_naive_truncation`.

The governor paces *before* spending rather than backing off after, because a
429 costs quota too. It reserves an estimate, then reconciles against reported
usage in both directions — refunds matter, since reserving the completion cap
in full would idle most of the quota on tokens no request actually spends.

---

## Layout

```
src/toolkit_recon/
  schema.py      the contract: AppResearch (output) + Extraction (LLM)
  apps.py        the 100 apps, each with its official domains
  providers.py   Composio search/fetch, and the keyless fallback
  ranking.py     official-docs preference, blogspam down-ranking
  condense.py    relevance-based excerpting to fit the token budget
  extract.py     strict JSON-schema structured output
  ratelimit.py   FIFO sliding-window token governor
  confidence.py  the confidence rules, in code
  throttle.py    per-domain politeness + retry classification
  storage.py     raw evidence, JSONL trace, atomic checkpoints
  pipeline.py    orchestration and per-app isolation
  cli.py         entry point and run summary
tests/           31 tests, no network required
```

---

## Known limits

Stated plainly, because a reviewer will find them anyway:

* **Three documents per app.** Enough to settle auth and access tier; not
  enough to characterise a large API surface, so `api_breadth` is the softest
  field in the schema and should be read as a coarse signal.
* **`has_mcp: false` means "no evidence found in the pages we fetched"**, not
  "no MCP server exists". The MCP ecosystem moves faster than vendor docs, and
  a two-query search is a weak instrument for it. Treat false as unknown.
* **`pass_number` exists for an accuracy delta across re-runs.** Only pass 1
  is included here; passes 2 and 3 are for re-running the low-confidence rows
  with different queries and measuring how many claims move.
* **Single extraction model, no ensemble.** Disagreement between two models
  would be a stronger conflict signal than one model's `signal_sources_conflict`.
* The 8,000 TPM quota, not the network, sets the floor on run time.

---

## Configuration

All optional; defaults are sized for the tier this was developed against.

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSIO_API_KEY` | — | Composio project key; enables the Composio provider |
| `COMPOSIO_USER_ID` | `toolkit-recon` | Session identity |
| `GROQ_API_KEY` / `LLM_API_KEY` | — | Extraction model credential |
| `LLM_BASE_URL` | Groq | Any OpenAI-compatible endpoint |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Must support strict JSON-schema output |
| `LLM_TOKENS_PER_MINUTE` | `8000` | Raise on a larger tier for a faster run |
| `LLM_CONCURRENCY` | `2` | In-flight extraction calls |
| `TOOLKIT_RECON_CONCURRENCY` | `8` | Global app-level concurrency |
| `TOOLKIT_RECON_DOMAIN_DELAY` | `1.5` | Seconds between hits on one host |
