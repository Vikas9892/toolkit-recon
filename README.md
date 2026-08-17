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

Verification artifacts:

| Path | Contents |
|---|---|
| `data/validation_report.json` | Layer 1: per-rule counts, affected apps, re-run queues |
| `data/pass1.validated.json` | Pass 1 with Layer 1 corrections applied |
| `data/pass2.json` | Layer 2: re-checked rows, corroborated or disputed |
| `data/disagreements.json` | Every field where the two passes disagreed |
| `data/pass3.json` | Layer 3: browser-verified resolutions |
| `evidence/screenshots/{slug}.png` | What the browser actually saw |
| `data/audit_queue.csv` | Phase 3: 20 stratified apps for a human to adjudicate |
| `data/human_audit.json` | Phase 3: precision by field and by confidence |
| `data/accuracy_progression.json` | Convergence (measured) + accuracy (audited) |

---

## Quick start

```bash
pip install -r requirements.txt
pip install -e .

cp .env.example .env      # add COMPOSIO_API_KEY and GROQ_API_KEY

python -m toolkit_recon --pass-number 1          # all 100 apps
python -m toolkit_recon --only slack,stripe      # a couple, for a smoke test
python -m toolkit_recon --resume                 # continue after an interrupt
pytest -q                                        # 34 unit tests, no network
```

Then read the results:

```bash
python -m toolkit_recon.report                   # distributions + process metrics
python -m toolkit_recon.report --triage          # the human review queue
```

### The three verification layers

Each layer is progressively more expensive and progressively narrower in
scope. The rule that holds across all three: **no layer accepts a trust signal
the model supplied.**

```bash
# Layer 1 — structural validation (seconds, no network)
python -m toolkit_recon.validate

# Layer 2 — independent second pass over weak + flagged rows
python -m toolkit_recon --pass-number 2 --recheck-from 1 \
    --recheck-file pass1.validated.json --include-flagged --out pass2.raw.json
python -m toolkit_recon.corroborate

# Layer 3 — browser verification, disputed fields only
python -m playwright install chromium
python -m toolkit_recon.browser_verify

# The progression
python -m toolkit_recon.progression
```

**Layer 1 — structural validation** (`validate.py`). Eight deterministic rules
over the row, the trace, and the archived evidence. Actions can `force_low` or
`downgrade` one level, never upgrade — pinned by a test. Writes
`validation_report.json` and `pass1.validated.json`; `pass1.json` is left
untouched, because the progression has to measure the raw first pass as it
actually was.

| Rule | Catches | Action |
|---|---|---|
| R1 | `evidence_urls` empty | force low |
| R2 | cited a URL we never fetched | flag |
| R3 | `has_mcp` without a page that mentions MCP | flag |
| R4 | `buildable=yes` vs `partner_gated`/`no_public_api` | flag |
| R5 | `api_style=[None]` vs `buildable=yes` | flag |
| R6 | `auth_methods=[Unknown]` | force low |
| R7 | all evidence off official domains | downgrade |
| R8 | all archived docs below `min_doc_chars` | flag |

**Layer 2 — independent second pass** (`corroborate.py`). Pass 2 changes both
the search queries *and* the extraction framing. Pass 1 asks what the product
offers; pass 2 asks what will break when you try to ship it. Agreement between
two runs reasoning from opposite priors is corroboration; agreement between a
run and a copy of itself is not. All compared fields agreeing promotes
confidence one level (capped at `high`); any disagreement marks the field
disputed and routes the row to Layer 3. Writes `pass2.json` and
`disagreements.json`.

**Layer 3 — browser verification** (`browser_verify.py`). Disputed fields
only. Playwright loads the official docs URL, scrolls lazy content into the
DOM, and captures both text and a screenshot to
`evidence/screenshots/{slug}.png`. The judge model must return a **verbatim
quote**, and `quote_is_grounded` checks that quote literally occurs in the text
just captured. An ungrounded quote resolves nothing — the field records
`unresolvable`, so an invented citation fails closed rather than settling a
dispute in favour of a fabrication. Resolutions: `pass1_correct`,
`pass2_correct`, `both_wrong`, `unresolvable`.

### Convergence is not accuracy

The two words mean different things here and the code keeps them apart:

| Term | What it measures | Where it comes from |
|---|---|---|
| **`convergence_rate`** | How often the passes agree with *each other* | The chain, from artifacts on disk |
| **`accuracy`** | How often the agent matches *ground truth* | Phase 3 human audit only |

Convergence measures internal consistency and nothing else. **An agent that is
consistently wrong scores 100%.** Three identical wrong answers converge
perfectly. That is why the chain's pass1→pass2→pass3 figure is labelled
`convergence_rate` everywhere in code and output, and why `accuracy` appears in
exactly one place — the `accuracy` block of `accuracy_progression.json`, fed
only by `data/human_audit.json`. A test asserts no pass block carries an
`accuracy` key.

### Phase 3 — the human audit

```bash
python -m toolkit_recon.audit              # -> data/audit_queue.csv (20 apps)
#   a human reads the vendor docs and fills the verdict_* columns
python -m toolkit_recon.report --audit     # -> data/human_audit.json
python -m toolkit_recon.progression        # folds it into the accuracy block
```

**The sample** (`audit.py`) is stratified, seeded, and reproducible: exactly 20
apps, 10 high-confidence and 10 medium/low, spread across categories with a
per-category cap derived from how many categories exist. It runs on partial
data, so the queue can be built while pass 1 is still going.

The 50/50 split is the whole point. Auditing only the rows the pipeline already
doubts would confirm what we know and hide what we don't — **systematic
overconfidence is visible only in the high-confidence rows.**

**Verdict vocabulary**: `correct` | `partially_correct` | `wrong` |
`unverifiable`.

**The headline result** is `precision_by_confidence`: high vs medium/low. If
high-confidence rows are not measurably more accurate than the rest, the
confidence column is decoration, and the report says so in those words rather
than burying it. `precision_by_field` is expected to be uneven — `access_tier`
is the hardest field and should score worst.

`unverifiable` is excluded from the precision denominator rather than counted
as a miss: a field no public document can settle is a fact about the vendor's
documentation, not an error by the agent. It is reported separately so the
exclusion is visible instead of flattering. `precision_lenient` credits
`partially_correct` as half.

> **Known gap.** The brief asked to force-include Amazon SP-API, PitchBook, and
> Salesforce Commerce Cloud as gated products that stress `access_tier`. None
> of the three is in the 100-app corpus, so no pass-1 row exists to audit. The
> sampler reports them under `forced_missing` and does **not** silently
> substitute an easier app, which would hide that the hardest field went
> untested. To include them: add them to `APPS`, profile with
> `python -m toolkit_recon --only <slug>`, then rebuild the queue.

### What the progression file deliberately does not claim

`accuracy_progression.json` ships with its whole `accuracy` block **null**.

That is the point. A pipeline that scores its own correctness is measuring
self-consistency, not accuracy. Inter-pass agreement is the tempting proxy and
it is the wrong one: two passes can agree and both be wrong, which is exactly
what the `both_wrong` resolution exists to catch. Every figure that is a matter
of record — rows touched, flagged, promoted, disputed, resolved, and the
`convergence_rate` itself — is computed from artifacts on disk. The accuracy
numbers wait for a human.

### Re-check passes and the field-level delta

`pass_number` is not decoration — it drives a second look at the rows the
first pass was not entitled to be sure about:

```bash
# Re-profile every low/medium row from pass 1, as pass 2
python -m toolkit_recon --pass-number 2 --recheck-from 1

# What actually moved?
python -m toolkit_recon.report --delta 1 2
```

Each pass issues **different queries** (`QUERY_SETS` in `pipeline.py`). Reusing
pass 1's queries would hit the same cache and reproduce the same row, which
measures nothing. Pass 2 asks about developer portals and access requirements;
pass 3 asks about MCP and OAuth scopes. Fields that move between passes are
exactly the claims a single pass should not have been trusted on.

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
  pipeline.py    orchestration, per-app isolation, per-pass query sets
  cli.py         entry point and run summary
  report.py      post-run analysis, triage queue, pass-to-pass delta
  validate.py       LAYER 1  structural rules over row + trace + evidence
  corroborate.py    LAYER 2  inter-pass agreement, disputes, promotion
  browser_verify.py LAYER 3  Playwright + grounded-quote verification
  audit.py          PHASE 3  stratified sampler -> audit_queue.csv
  progression.py    convergence (measured) vs accuracy (audited)
tests/           86 tests, no network required
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
* **Single extraction model, no ensemble.** Disagreement between two models
  would be a stronger conflict signal than one model's `signal_sources_conflict`.
* **The delta is a churn measure, not a correctness measure.** `--delta` shows
  which claims moved between passes, which is a good proxy for instability but
  is not ground truth. Only a hand-labelled sample would give real accuracy,
  and that is the obvious next step.
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
