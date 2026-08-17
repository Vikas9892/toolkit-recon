# toolkit-recon

<!-- Draft skeleton. Replaces the root README once pass 1 lands and the
     headline finding can be written from data rather than guessed. -->

## 1. What this is

A research agent that profiles 100 SaaS applications for **agent-toolkit
buildability** — could an engineer ship a toolkit that lets an AI agent call
this product's API today, and if not, what stops them? Search and page
retrieval run through Composio; extraction is schema-constrained, and the
confidence attached to every row is computed by the pipeline from what it
actually fetched rather than asserted by the model.

**Live findings:** TODO — link once published.

---

## 2. Headline finding

> **TODO.** Blocked on pass 1 completing. This section gets one number and one
> sentence, both from data:
>
> - the buildable/gated split across 100 apps, and
> - the human-audited precision gap between `high` and `medium/low` confidence
>   rows, which is what says whether the confidence column carries signal.
>
> It will not be written from convergence figures. See §5 for why those are a
> different quantity.

---

## 3. Quickstart

**Verified** from a cold clone into a fresh virtualenv: dependencies install,
all 91 tests pass with no network and no `.env`, and the CLI refuses to start
without a Composio key rather than silently degrading.

```bash
git clone https://github.com/Vikas9892/toolkit-recon.git
cd toolkit-recon

python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
python -m playwright install chromium      # only needed for Layer 3

cp .env.example .env          # then fill it in
```

`.env` must be **UTF-8 without a BOM**. A BOM makes the first variable in the
file parse with an invisible prefix, and the key silently disappears; the run
now refuses to start rather than quietly falling back.

Required keys:

| Variable | Purpose |
|---|---|
| `COMPOSIO_API_KEY` | Composio Platform project key (`ak_…`), powers search/fetch |
| `GROQ_API_KEY` or `LLM_API_KEY` | Extraction model credential |
| `LLM_MODEL` | Must support strict JSON-schema output. Verified: `openai/gpt-oss-20b`, `openai/gpt-oss-120b`. `qwen/qwen3.6-27b` fails. |

Then:

```bash
python -m toolkit_recon --pass-number 1        # all 100 apps
python -m toolkit_recon --only slack,stripe    # smoke test
python -m toolkit_recon --resume               # continue after an interrupt
python -m toolkit_recon.report                 # distributions + process metrics
pytest -q                                      # TODO: final count; no network
```

---

## 4. Architecture

```
search (2 queries)  ->  rank  ->  fetch top 2-3  ->  extract  ->  score  ->  persist
```

Both network primitives run through Composio: `COMPOSIO_SEARCH_WEB` and
`COMPOSIO_SEARCH_FETCH_URL_CONTENT`, both Composio-managed (`no_auth: true`).
Tool slugs and toolkit versions are discovered at runtime and **pinned for the
whole run**, printed at startup, so a mid-run toolkit release cannot silently
change the extraction inputs.

### The `Extraction` / `AppResearch` split

Two models in `schema.py`, and the boundary between them is the design:

- **`AppResearch`** — the output row.
- **`Extraction`** — the complete set of things the LLM is *permitted* to say.

Anything absent from `Extraction` is something the model cannot influence.
`confidence`, `pass_number`, and the final `evidence_urls` are all absent.

The model does not rate its own certainty, because a model asked how sure it is
rates its own fluency. It supplies three observations instead —
`signal_auth_in_official_docs`, `signal_tier_explicitly_stated`,
`signal_sources_conflict` — and `confidence.py` scores those.

On top of that, `official_docs_reached` is **measured**: true only when a fetch
against a domain declared official for that app actually returned text above
`min_doc_chars`. A model claiming it read the official auth section on a run
where no vendor page was retrieved still yields `low`.

The same rule runs downstream. Evidence URLs are intersected with pages
actually fetched. `has_mcp` survives only if the cited page was fetched *and*
mentions MCP.

### Three verification layers

| Layer | What it does | Trust anchor |
|---|---|---|
| 1 — `validate.py` | 8 structural rules over row, trace, archived evidence | Reads only disk artifacts; can downgrade, never upgrade |
| 2 — `corroborate.py` | Second pass with different queries *and* a different extraction lens | Promotion earned by measured inter-pass agreement |
| 3 — `browser_verify.py` | Playwright re-reads disputed fields on the live page | Verdict discarded unless its quote occurs verbatim in the captured DOM |

---

## 5. How accuracy was measured

Two quantities, kept apart on purpose:

| Term | Measures | Source |
|---|---|---|
| `convergence_rate` | How often the passes agree with **each other** | The chain |
| `accuracy` | How often the agent matches **ground truth** | Human audit only |

**A consistently-wrong agent scores 100% convergence.** If the pipeline
misreads a pricing page the same way twice, both passes agree, and the row
converges perfectly while being wrong. Convergence measures stability, not
correctness — so `accuracy` lives in its own block of
`accuracy_progression.json`, fed only by `human_audit.json`, and a test asserts
no pass block carries an `accuracy` key.

**The audit** (`audit.py`) draws a seeded, reproducible sample of 20 apps: 10
high-confidence and 10 medium/low, spread across categories. The 50/50 split is
the point — auditing only rows the pipeline already doubts would confirm what
we know and hide systematic overconfidence, which is visible only in the
high-confidence rows.

Verdicts are `correct | partially_correct | wrong | unverifiable`.
`unverifiable` is excluded from the precision denominator and reported
separately: a field no public document can settle is a fact about the vendor's
documentation, not an agent error.

**The headline is `precision_by_confidence`** — high versus medium/low. If high
rows are no more accurate than the rest, the confidence column is decoration,
and the report says so in those words.

---

## 6. Known limitations

- **Three forced-include apps are missing.** Amazon SP-API, PitchBook, and
  Salesforce Commerce Cloud were requested for the audit sample because they
  stress `access_tier`, the hardest field. None is in the corpus. They are
  reported under `forced_missing` rather than substituted — a substitution
  would give a cleaner sample that measures less. **Coverage of hard gating
  cases is therefore weaker than intended.**
- **`min_doc_chars` landed mid-run** and is applied retroactively against
  archived manifests, re-running only the apps whose evidence was entirely
  thin. Recorded as a process note in `validation_report.json`.
- **Fields no public document can settle.** Some vendors never state which plan
  grants API access. Those become `unverifiable`, not wrong.
- **Three documents per app.** Enough to settle auth and access tier; not
  enough to characterise a large API surface, so `api_breadth` is the softest
  field and should be read as a coarse signal.
- **`has_mcp: false` means "no evidence in the pages we fetched"**, not "no MCP
  server exists".
- **Single extraction model, no ensemble.** Disagreement between two models
  would be a stronger conflict signal than one model's self-reported
  `signal_sources_conflict`.
- **A provider daily-token cap bounded what could be run in one day.** The
  governor models per-minute limits; the per-day wall surfaces as
  `DailyQuotaExhausted` and aborts with the checkpoint intact.

---

## 7. Repo layout

```
src/toolkit_recon/
  schema.py         the contract: AppResearch (output) + Extraction (LLM)
  apps.py           the 100 apps, each with its official domains
  providers.py      Composio search/fetch, and the keyless fallback
  ranking.py        official-docs preference, blogspam down-ranking
  condense.py       relevance-based excerpting to fit the token budget
  extract.py        strict JSON-schema structured output
  ratelimit.py      FIFO sliding-window token governor
  confidence.py     the confidence rules, in code
  throttle.py       per-domain politeness, retry classification, quota errors
  storage.py        raw evidence, JSONL trace, atomic checkpoints
  pipeline.py       orchestration, per-app isolation, per-pass query sets
  cli.py            entry point and run summary
  report.py         analysis, triage queue, pass delta, audit scoring
  validate.py       LAYER 1  structural rules
  corroborate.py    LAYER 2  inter-pass agreement
  browser_verify.py LAYER 3  Playwright + grounded-quote verification
  audit.py          PHASE 3  stratified sampler -> audit_queue.csv
  progression.py    convergence (measured) vs accuracy (audited)
docs/
  run-notes.md      what the run taught me
  defence.md        design decisions traced to code (not for submission)
```

---

## 8. Tests

**TODO: final count** (91 at time of writing). They need **no network** — every
external boundary is either pure logic or fed a fixture.

They pin the properties that are easy to regress silently:

- confidence cannot be talked upward by the model
- validation rules can downgrade but never upgrade
- a cancelled rate-limiter waiter cannot leak quota
- a narrow run cannot erase a wider checkpoint
- condensation beats naive truncation on a page where the evidence sits mid-document
- an ungrounded browser quote resolves nothing
- no pass block carries an `accuracy` key
