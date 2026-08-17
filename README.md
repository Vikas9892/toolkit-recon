# toolkit-recon

A research agent that profiles 100 SaaS applications for **agent-toolkit
buildability** — could an engineer ship a toolkit that lets an AI agent call
this product's API today, and if not, what stops them?

Built for an AI Product Ops take-home at Composio. The brief's constraint was
*accuracy matters more than coverage*, so the design decisions below all trade
throughput and breadth for auditability.

**Deliverable:** [toolkit-recon.vercel.app](https://toolkit-recon.vercel.app) —
one page, generated from `data/` at build time.

---

## Headline finding

**63 of 100 apps profiled.** The run ended on `DailyQuotaExhausted` at
196,846/200,000 tokens. Every figure in this repo is over 63, not 100.

Of those 63, **48 report as self-serve. Hand-checking says that is a ceiling,
not a count.**

| | |
|---|---|
| Pipeline said **gated** | correct **4 of 4** times |
| Pipeline said **self-serve** | wrong **1 of 3** scoreable rows |

The miss — Salesloft — shows the mechanism: developer documentation describing
*how to authenticate* was read as evidence that credentials are *obtainable*,
for a vendor that publishes no pricing at all. The errors run one way. The
method over-reports reachability and never under-reports it.

The only apps it reliably identifies as unreachable are the 9 with
`no_public_api` — which are also the only 9 with narrow API surfaces. **It
detects the absence of an API. It does not detect the presence of a paywall.**

A fourth app broke the schema rather than the pipeline. BILL's access tier
depends on which product you mean: Enterprise for AP/AR, free for the API
platform. Neither enum value is correct, because **access tier is a property of
a product, not a company.** Recorded as `schema_cannot_express`; no enum member
was added.

Three things this finding is not:

* **Not a magnitude.** The sample is 3 scoreable rows, deliberately enriched
  with expected-gated products. It bounds the *direction* of the error, not its
  size.
* **Not an accuracy figure.** No accuracy number exists in this project. The
  19-row audit queue was generated and never filled, and a test asserts no
  output file carries a populated `accuracy` key. What the artifacts carry is
  cross-model agreement, which is a different quantity — see
  [Convergence is not accuracy](#convergence-is-not-accuracy).
* **Not something the cross-tab could have found.** The tier × confidence
  cross-tab returned a null (89% vs 91%). A bias uniform across confidence
  cohorts produces a null there *by construction* — it can only detect a bias
  that concentrates in the weak cohort.

**Implication for toolkit prioritisation:** `access_tier` needs a second
evidence source, and for multi-product vendors it needs a different research
unit — the API product, not the app.

One further result worth stating, because it cost something to learn: before
the human check, an agent-filled pass read the same vendor pages and got **2 of
4 wrong**, calling Gorgias and Deel misses when the pipeline was right on both.
That is direct evidence a second model reading the same class of page is not
independent verification. The artifact carried that caveat on its own face
before there was any way to test it, and the caveat was correct.

---

## What it produces

| Path | Contents |
|---|---|
| `data/pass1.json` | 63 rows, one per app, validated against `AppResearch` |
| `data/raw/{slug}/` | Every fetched page as text, plus a `manifest.json` |
| `logs/trace.jsonl` | One execution trace per app: queries, URLs, tokens, wall time, confidence |
| `data/checkpoints/` | Rewritten after every app, so a crash never costs the run |

Verification artifacts:

| Path | Contents | State |
|---|---|---|
| `data/validation_report.json` | Layer 1: per-rule counts, affected apps, re-run queues | 2 violations / 63 rows |
| `data/pass1.validated.json` | Pass 1 with Layer 1 corrections applied | 63 rows |
| `data/pass2.json` | Layer 2: re-checked rows, corroborated or disputed | 4 usable readings |
| `data/corroboration_summary.json` | Layer 2 coverage, and **every unverified row by name** | 59 unverified |
| `data/disagreements.json` | Every field where the two passes disagreed | 25 disagreements |
| `data/pass3.json` | Layer 3: browser-verified resolutions | **not produced — skipped, see below** |
| `data/hand_check_queue.csv` | Access tier vs the vendor's own pricing page | 8 human-verified |
| `data/hand_check.json` | Error rates by direction, per-app verdicts, schema gaps | the headline finding |
| `data/patterns.json` | Phase 4: archetypes, blocker families, auth, category, MCP | — |
| `data/audit_queue.csv` | Phase 3: stratified apps for a human to adjudicate | **generated, never filled** |
| `data/human_audit.json` | Phase 3: precision by field and by confidence | **does not exist** |
| `data/accuracy_progression.json` | Convergence (measured) + accuracy (audited) | accuracy is `null` |
| `site/index.html` | The deliverable, generated from all of the above | [live](https://toolkit-recon.vercel.app) |

The blanks are deliberate. A file that does not exist because the work was not
done is a different artifact from one full of zeroes, and nothing downstream
reads the first as the second.

---

## Quick start

```bash
pip install -r requirements.txt
pip install -e .

cp .env.example .env      # add COMPOSIO_API_KEY and GROQ_API_KEY

python -m toolkit_recon --pass-number 1          # all 100 apps
python -m toolkit_recon --only slack,stripe      # a couple, for a smoke test
python -m toolkit_recon --resume                 # continue after an interrupt
pytest -q                                        # 142 unit tests, no network
```

Then read the results:

```bash
python -m toolkit_recon.report                   # distributions + process metrics
python -m toolkit_recon.report --triage          # the human review queue
python -m toolkit_recon.report --findings        # cross-tabs, blockers, deadlines
python -m toolkit_recon.patterns                 # PHASE 4  pattern clustering
python -m toolkit_recon.site                     # build site/index.html
```

### Is the access tier real?

The one check that can separate *"the corpus really is mostly self-serve"* from
*"the extractor reads documented API as obtainable credentials"*. Nothing else
in the repo can — see the [headline finding](#headline-finding).

```bash
# Stage the rows a human should check against vendor pricing pages.
python -m toolkit_recon.report --hand-check

# ... a person fills truth_access_tier / truth_evidence_url / why_it_failed,
#     and sets truth_source=human. Only human rows are ever scored.

python -m toolkit_recon.report --hand-check-score   # -> data/hand_check.json
```

`--hand-check` refuses to overwrite a queue whose truth column is already
filled; regenerating is cheap and the fill is not. `--force` overrides it.

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

# Layer 3 — browser verification, disputed fields only  (NOT RUN, see below)
python -m playwright install chromium
python -m toolkit_recon.browser_verify

# The progression
python -m toolkit_recon.progression
```

**What the layers actually reached on this run:**

| Layer | Coverage | Result |
|---|---|---|
| 1 — structural | 63/63 | 2 violations. Braze `has_mcp` **retracted** (evidence on `apis.io`, not a vendor domain); Telegram `api_style` contradiction flagged |
| 2 — second pass | **4/63 (6%)** | 11 attempted, 7 failed, 52 never reached. 0 promotions, 25 field disagreements. All 59 unverified rows named in `corroboration_summary.json` |
| 3 — browser | **skipped** | Deliberate. Recorded in [`docs/run-notes.md`](docs/run-notes.md) with the reason |

Layer 3 settles disagreements *between two passes*, so its ceiling is inter-pass
consistency — and both passes share the extractor whose bias turned out to be
the actual finding. It cannot see a bias both passes have. The hand check
answered the bigger question directly. The 11-row queue is preserved in
`corroboration_summary.json` as a queue that was **not run**, which is a
different artifact from one that came back clean.

The honest footnote: the token budget was exhausted by that point anyway. The
decision is what I would make with budget in hand, but I did not have to make it
with budget in hand, and those are different claims.

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
python -m toolkit_recon.audit              # -> data/audit_queue.csv
#   a human reads the vendor docs and fills the verdict_* columns
python -m toolkit_recon.report --audit     # -> data/human_audit.json
python -m toolkit_recon.progression        # folds it into the accuracy block
```

> **This queue was generated and never filled.** That is why no accuracy figure
> appears anywhere in this repo. The steps below describe what would happen if
> it were.

**The sample** (`audit.py`) is stratified, seeded, and reproducible. Both the
size and the strata are derived from the corpus, never hardcoded: 30% of N,
floored at 5 per stratum so the high-vs-weak precision comparison is possible at
all, capped at 25 because past that a human stops adjudicating carefully. At 63
rows that is **19 apps, 9 high-confidence and 10 medium/low**, spread across
categories with a per-category cap derived from how many categories exist.

A fixed 20 would have been a different sample against 63 rows than against 100 —
silently a larger share of a smaller corpus while still reading as "20 rows".
The derivation and whether it was overridden are recorded in
`audit_sample_meta.json`. It runs on partial data, so the queue can be built
while pass 1 is still going.

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
  coverage.py    category-balanced queue ordering when budget is short
  validate.py       LAYER 1  structural rules over row + trace + evidence
  corroborate.py    LAYER 2  inter-pass agreement, disputes, promotion
  browser_verify.py LAYER 3  Playwright + grounded-quote verification
  tier_audit.py     is access_tier real? cross-tab, pricing evidence,
                    hand-check queue + scorer  <- the headline finding
  audit.py          PHASE 3  stratified sampler -> audit_queue.csv
  patterns.py       PHASE 4  archetypes, blocker families, MCP cluster
  progression.py    convergence (measured) vs accuracy (audited)
  site.py           builds site/index.html from data/, nothing hand-typed
tests/           142 tests, no network required
site/            the built deliverable + the raw JSON behind every figure
```

---

## Known limits

Stated plainly, because a reviewer will find them anyway.

**What this run hit, not what it might hit:**

* **63 of 100 apps.** `DailyQuotaExhausted` at 196,846/200,000 tokens. Every
  figure in this repo is over 63. 9 of the 63 are `RESEARCH FAILED` rows carried
  in the corpus rather than dropped — a missing row and a failed row are
  different facts.
* **`access_tier` over-reports reachability.** Measured directionally against
  vendor pricing pages. It is a ceiling on self-serve, not a count, and it needs
  a second evidence source before it drives any prioritisation.
* **`access_tier` has the wrong unit for multi-product vendors.** BILL has
  Enterprise-gated AP/AR APIs and a free API platform; no enum value is correct.
  The research unit should be the API product, not the app. Recorded, not
  patched — changing the enum mid-project invalidates every row already
  collected.
* **No accuracy figure exists.** The 19-row audit queue was generated and never
  filled. Convergence is not accuracy, and a test enforces that no output file
  claims otherwise.
* **Layer 2 reached 6%.** Layer 3 was skipped. Both are stated above with which
  rows went unverified.
* **The 18% pricing-evidence figure is a lower bound**, and over the final
  corpus it is 17% (8 of 48 self-serve rows whose evidence never mentions
  pricing). Keyword presence is not the page stating a tier, so a nav-bar
  "Pricing" link still counts as evidence. The true figure is higher.
* **Deadline instrumentation disagrees with itself.** All 5 deadline hits were
  in stage `extract`. Pass 1's two fired at 600.01s exactly; pass 2's three
  recorded 1271.99s, 1400.13s and 1271.86s against the same 600s bound —
  2.1–2.3×. Undiagnosed, and stated rather than buried.
* **Design 2, E-signature 1, Scheduling 1.** Too thin for a per-category claim.
  A corpus-design weakness, not a scheduling one.
* **Amazon SP-API, PitchBook and Salesforce Commerce Cloud are not in the
  corpus**, so the hardest access-tier cases went untested. Recorded in
  `audit_sample_meta.json` and `hand_check_meta.json` rather than substituted.

**Method limits that would apply on a complete run too:**

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
  is not ground truth. Only a hand-labelled sample gives real accuracy — the
  hand check is that, for one field, at three rows.
* **A second model is not a second opinion.** An agent-filled pass over the
  same vendor pages got 2 of 4 wrong against human verification. Cross-model
  agreement is worth reporting and is not verification.
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
| `LLM_MODEL` (as run) | `openai/gpt-oss-20b` | The corpus was built on `20b` after `120b`'s daily budget was spent. Rows carry `extracted_by` and traces carry `llm_model`, so a split cohort would be visible if one occurred. It did not — the corpus is uniform |
| `LLM_TOKENS_PER_MINUTE` | `8000` | Raise on a larger tier for a faster run |
| `LLM_CONCURRENCY` | `2` | In-flight extraction calls |
| `TOOLKIT_RECON_CONCURRENCY` | `8` | Global app-level concurrency |
| `TOOLKIT_RECON_DOMAIN_DELAY` | `1.5` | Seconds between hits on one host |
