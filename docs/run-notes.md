# What the run taught me

Notes written while debugging, not reconstructed afterwards. Numbers are the
ones I actually measured.

---

## a) The confidence column was gameable

The schema asks for `confidence`, and the obvious implementation is to let the
extraction model return it alongside everything else. I did that first. It was
wrong, and the reason is not subtle: a model asked to rate its own certainty
rates its own fluency. It had just written three confident paragraphs, so it
said `high`.

The rules I was given are specific — `high` needs an auth section found in
official docs and an explicitly stated tier. Both halves are checkable. So I
stopped asking the model for a verdict and started asking it for observations.
`Extraction` now carries three booleans: `signal_auth_in_official_docs`,
`signal_tier_explicitly_stated`, `signal_sources_conflict`. `confidence.py`
scores those into a level. The model reports what it saw; the code decides what
that is worth.

That alone is not enough, because the model can still overclaim on the first
signal. So `official_docs_reached` became something the pipeline *measures*, not
something anyone asserts: it is true only when a fetch against a domain declared
official for that app in `apps.py` actually came back with text above
`min_doc_chars`. If we never retrieved a vendor page, the row is capped at
`low` no matter what the model says about it.

The test that guards this is
`test_no_official_docs_forces_low_even_if_model_claims_otherwise`. It hands
`assign_confidence` an extraction claiming it read the auth section in official
docs, with `official_docs_reached=False`, and asserts the result is `low`. Code
wins the argument.

The same pattern runs through the rest. `evidence_urls` are intersected with
the set of pages actually fetched, and a citation outside that set is dropped
with a note. `has_mcp` survives only if the cited page is one we retrieved *and*
that page mentions MCP.

---

## b) Blind truncation was throwing away the evidence

Every extraction has a token budget, and the lazy way to fit it is `text[:n]`.
I did that first too.

Docs pages do not put the useful part first. They open with navigation, a
product pitch, a quickstart. The authentication section sits somewhere in the
middle. So a leading slice reliably keeps the marketing and discards the exact
paragraphs the confidence rules depend on — and the failure is silent, because
the model still returns a confident-looking row built from the parts that
survived.

`condense.py` scores paragraphs against schema-relevant keywords — weighted, so
`model context protocol` counts for 8 and `sdk` for 2 — keeps a short lead for
product identity, then takes the highest-scoring blocks until the budget is
spent and reassembles them **in document order** with `[... omitted ...]`
markers. Document order matters: a bag of high-scoring fragments reads as noise,
and the marker lets the model tell a gap from an absence.

`test_condense_beats_naive_truncation` builds a page where the auth section sits
past the cut, asserts `"OAuth2" not in doc[:900]`, and asserts it survives
condensation.

Finding this also surfaced a bug in the fix: the lead was a fixed 900
characters, so at a 900-character budget it consumed everything and no scored
block ever fit. The lead is now capped at a third of the budget.

---

## c) The token governor: three fixes, each one surfacing the next

The extraction endpoint allows 8,000 tokens per minute. That, not the network,
sets the pace of the whole run. Eight workers hit it instantly.

**Starvation.** My first limiter released its lock while sleeping. Every
blocked worker therefore woke on the same window roll, raced for the one slot
that had opened, and the losers went back to sleep for another full window. In
a five-app smoke test one app waited **4,063 seconds to do 3,216 tokens of
work**. I made admission strictly FIFO.

**Throughput.** With FIFO in place the pacing was correct but slow — one app a
minute. The arithmetic: a call reserved about 3,900 tokens, two of those exceed
a 7,600-token effective capacity, so only one was ever admitted per window. I
trimmed the per-call document budget from 6,000 to 5,400 characters and the
search hint from 1,200 to 600. Two calls now fit. Throughput doubled for about
ten percent less context.

**Ghost tickets.** Then the run wedged. A cancelled or timed-out `acquire()`
left its ticket sitting in the FIFO queue. The scheduler would later reach that
ticket, admit it, and charge its tokens against the window — quota spent on a
request nobody was ever going to send. Real waiters starved behind a ghost. The
leak was permanent and compounding, so the run degraded rather than failing.

I reproduced it at **`in_window: 6691` of 7,600, `queued: 3`, nothing running**.
The head needed 3,345 and 6,691 + 3,345 is over capacity, so admission could
never happen again.

`acquire()` now accounts for its ticket on every exit path through a `finally`,
and refunds the reservation if the ticket won its slot after the caller had
already gone. Three regression tests: cancellation, `max_wait` timeout, and that
the queue still drains and real work still gets through afterwards.

---

## d) Pass 1 stalled, and I chased the wrong cause twice

What I saw: the run went quiet for fifteen minutes. Checkpoint frozen at 51
rows. CPU 0.03 seconds per 12 seconds of wall clock — blocked, not spinning.
Four connections open, nothing completing.

What I ruled out, in order. Groq was healthy: 200 in 0.8 seconds, with 7,927
tokens and 960 requests showing as remaining. Composio was healthy too — eight
concurrent searches through one shared client finished in 3.8 seconds. Evidence
had been archived for `iterable` at 22:30, which put the block after fetching
and inside extraction.

I blamed the token limiter. Then I blamed httpx connection-pool starvation
inside the Composio SDK. Both were wrong, and I wrote a paragraph justifying
each before testing either.

Then I stopped theorising and reproduced it — three concurrent extractions
through one shared `Extractor`, printing the status of every stage. The answer
was sitting in the 429 body the whole time:

```
Rate limit reached ... on tokens per day (TPD): Limit 200000, Used 198529
```

A **200,000 token per-day cap**, sitting on top of the 8,000-per-minute cap my
governor modelled. The daily budget was gone. Every extraction 429'd, burned its
retry budget, and hit the per-app deadline.

**The transferable lesson is about the health check, not the limiter.** My probe
sent a one-token request and got a 200 back. A one-token request fits through a
nearly-spent daily bucket. A 3,300-token extraction does not. Worse, the
response headers report only the per-minute bucket, so the probe came back
green while the API was, for my actual workload, dead. A probe that does not
resemble the real request measures nothing — it measures the probe. The model
check I wrote afterwards sends a realistically sized prompt for exactly this
reason, and it is what caught that `qwen3.6-27b` fails strict JSON schema while
`gpt-oss-20b` passes.

The second lesson is about retries. `with_backoff` bounds *attempts*, not
*time*. A coroutine that never returns is never retried — it just holds its
concurrency slot. With all eight wedged, the run stopped without emitting a
single diagnosable error. A silent stall is worse than a crash: a crash tells
you where it happened. Bounded with `asyncio.wait_for` around the whole per-app
cycle at 600 seconds, which also frees the slot on cancellation, and around the
Composio SDK call, which runs through `to_thread` and had no timeout of its own.

And a per-day cap is not a rate to back off from. It is a wall. Retrying it
spends the run's remaining time producing identical failures. `DailyQuotaExhausted`
now aborts with the used-versus-limit numbers and stops starting new apps.

---

## Judgment calls, and what each one cost

**Prompt budget, 6,000 → 5,400 characters.** At the old size two extractions
would not fit in one 8,000-token window, so the governor admitted one per
minute. Halving the run time was worth roughly ten percent less context per
document, especially since condensation means the lost characters are the
lowest-scoring ones.

**`min_doc_chars` landed mid-run.** I added the thin-document floor after
watching a Close docs URL extract to 133 characters and still count as "official
docs reached". Rather than discard fifty minutes of completed work or pretend
the rule had always been there, `validate.py` applies it retroactively against
the archived manifests and queues only the apps it actually affects. The
reasoning is recorded in `validation_report.json` as a process note.

**Three forced-include apps are missing.** Amazon SP-API, PitchBook, and
Salesforce Commerce Cloud were requested for the audit sample because they
stress `access_tier`, the hardest field. None is in the 100-app corpus. The
sampler reports them under `forced_missing` and does not substitute an easier
app. A substitution would have produced a cleaner-looking sample that measured
less. **The consequence is real and should be stated in the writeup: the audit
sample's coverage of hard gating cases is weaker than intended.**

**The corpus has 14 categories, not 10.** I said 10 in an earlier note; that was
wrong. The sampler derives its per-category cap from the actual count rather
than a constant, so the stratification adapts either way.

**A three-app diagnostic destroyed 52 rows of completed work.** `Checkpoint`
loaded from disk only under `--resume`, but wrote unconditionally. My diagnostic
run did not resume, so it started from an empty dict and wrote its three rows
over the fifty-two already there. That is a design bug, not just operator error
— a checkpoint a narrower run can erase is not a checkpoint. Reading is now
unconditional; `--resume` decides only whether completed apps are *skipped*,
never whether they are *kept*.

**Model change at the restart.** With `gpt-oss-120b`'s daily budget spent and
the corpus needing a rebuild anyway, the whole of pass 1 moved to
`gpt-oss-20b`, which was the only candidate with both daily headroom and
working strict JSON schema output. Because every row is being regenerated,
there is no split cohort — the corpus is uniform. Rows carry `extracted_by` and
traces carry `llm_model` regardless, so a split would be visible if one ever
occurred.

---

## The Etsy false alarm

Reviewing early rows, I saw Etsy with `has_mcp: true` and an
`mcp_evidence_url` pointing at `.../getting_started/oauth` — an OAuth page. That
looked like exactly the failure I had been guarding against: a real URL attached
to an invented finding.

I grepped the archived evidence before saying anything. The page genuinely
documents an official MCP server: *"The OpenAPI Dev MCP server connects AI
coding assistants directly to the Etsy Open API."* The original answer was
correct and my suspicion was not.

I added the guard anyway. `has_mcp` now requires that the cited page both be one
we fetched and actually mention MCP, word-bounded so `McPherson` does not count.
Etsy passes it. The reason to keep the check is that a plausible citation
attached to an invented finding is precisely the failure a URL-only check cannot
see — the URL is real, the page is real, and only the claim about it is false.
