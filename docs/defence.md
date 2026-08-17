# Defence notes

Interview rehearsal, not submission material. Each answer traced to the code
that implements it. Where I cannot answer from the code without guessing, I say
so rather than filling the gap.

---

### Why is `Extraction` separate from `AppResearch`?

`schema.py` defines two models because they answer different questions.
`AppResearch` is the output contract — the row that lands in `pass1.json`.
`Extraction` is the *permission* contract: the complete set of things the LLM
is allowed to say. Anything absent from `Extraction` is, by construction,
something the model cannot influence.

That is what keeps `confidence`, `pass_number`, and the final `evidence_urls`
out of the model's hands. They are assembled in `Pipeline._profile_inner` from
things the pipeline observed. The split also means a schema change to the
output cannot silently widen what the model is trusted with — the two evolve
independently and on purpose.

---

### What does `official_docs_reached` measure, and why can't the model set it?

It is computed in `Pipeline._profile_inner` as
`{domain_of(d.url) for d in good if d.is_official}` — non-empty only when a
fetch against a domain listed in that app's `official_domains` (`apps.py`)
returned text at or above `settings.min_doc_chars`. So it is a statement about
bytes we retrieved, not about anything anyone believes.

`is_official` (`ranking.py`) matches the apex domain or a subdomain of it, so
`developers.notion.com` counts for `notion.com` while `notnotion.com` does not.

The model cannot set it because it is not a field of `Extraction`. The model
supplies `signal_auth_in_official_docs`, which `assign_confidence`
(`confidence.py`) will only honour when `official_docs_reached` is independently
true. `test_no_official_docs_forces_low_even_if_model_claims_otherwise` pins
exactly this: a model claiming it read official docs on a run where none were
retrieved yields `low`.

---

### Why is `MCP_MENTION` word-bounded?

`pipeline.py` defines it as `r"model context protocol|\bmcp\b"` with `re.I`.
The `\b` anchors exist because the bare token is three letters and appears
inside ordinary words — `McPherson` being the case in the test. Without the
boundary a page mentioning a person's name would validate an MCP claim.

The check runs against the text of the cited page, so an unbounded pattern
would convert a false positive in name matching directly into a false `has_mcp`
in the output. `test_mcp_mention_pattern_is_word_bounded` covers both
directions.

---

### Why is admission FIFO, and what was the ghost-ticket bug?

FIFO because the first design was not. `TokenRateLimiter.acquire` originally
released its lock while sleeping, so every blocked worker woke on the same
window roll, raced for one slot, and the losers slept another full window. One
app waited 4,063 seconds to do 3,216 tokens of work.
`test_no_starvation_under_contention` asserts admission order is `[0,1,2,3,4]`.

The ghost-ticket bug came from the fix. `acquire` enqueues a `_Ticket` and only
the queue head may be admitted; when a caller was cancelled or hit `max_wait`,
its ticket stayed in the queue. `_admit_head` would later reach that ticket,
pop it, and append its tokens to `_spent` — quota charged for a request nobody
would send. Reproduced at `in_window: 6691` of 7,600 with `queued: 3` and
nothing running, because the head needed 3,345 and could never fit again.

`acquire` now wraps its wait loop in `try/finally` and calls `_release`, which
either removes the ticket from the queue or, if it was admitted after the caller
left, refunds via `_refund`. `_release` is deliberately synchronous so it is
safe inside `finally` during cancellation, where awaiting a lock is not.

---

### Why 5,400 characters and not 6,000?

`settings.prompt_doc_budget`. At 6,000 characters of documents plus a 1,200
character search hint, one extraction reserved roughly 3,900 tokens. Two of
those exceed the effective capacity of `int(8000 * 0.95) = 7600`, so the
governor admitted exactly one call per 60-second window — one app a minute.

At 5,400 and a 600 character hint the reservation drops to about 3,500, two fit
in 7,600, and throughput doubles. The cost is roughly ten percent less document
context, and because `condense.py` drops the lowest-scoring blocks first, the
characters given up are the least relevant ones. Halving a fifty-minute run was
worth that.

---

### Why 600s and not 300s?

`settings.app_deadline`. The bound has to sit above the legitimate worst case or
it converts slow-but-healthy apps into failures. Under quota contention I
measured per-app wall times climbing to roughly 300 seconds, almost all of it
waiting for the token window rather than doing work. 600 gives roughly double
that headroom while still bounding a genuine hang to ten minutes.

`test_per_app_deadline_is_configured_above_observed_worst_case` asserts it stays
at or above 480 and above `request_timeout`, so the constant cannot drift down
into the range where normal contention starts tripping it.

---

### Why is `unverifiable` excluded from the precision denominator?

`_precision` in `report.py` computes over
`correct + partially_correct + wrong`, leaving `unverifiable` out and reporting
it as its own count.

The verdict means the auditor could not settle the field from any public
document. That is a fact about the vendor's documentation, not an error by the
agent, and scoring it as a miss would penalise the pipeline for a gap it cannot
close. Excluding it silently would be the flattering move, so the count is
reported alongside every precision figure and `precision_definition` ships in
the JSON. `precision_lenient` additionally credits `partially_correct` as 0.5,
so the strict and generous readings are both visible rather than one being
chosen for the reader.

---

### Why does a consistently-wrong agent score 100% convergence?

Because convergence only compares the agent against itself.
`progression.py` computes `convergence_rate` as
`fully_agreeing_rows / rows_compared` from `corroboration_summary.json`, and
`corroborate.compare` builds those numbers by checking whether pass 1 and pass 2
produced the same values for the same fields.

If the agent misreads a pricing page the same way twice, both passes say
`self_serve_free`, the fields agree, and the row counts as fully converged. The
metric is behaving correctly; it simply is not measuring correctness. That is
why `accuracy` lives in a separate block fed only by `human_audit.json`, why
both keys carry definition strings, and why
`test_progression_separates_convergence_from_accuracy` asserts no pass block
carries an `accuracy` key at all.

---

### Why does bounding a hang differ from retrying it?

`with_backoff` in `throttle.py` counts attempts. It can only act on a call that
*returns* — an exception is what triggers the next attempt. A coroutine that
never returns produces no exception, so the retry logic never runs and the
worker holds its concurrency slot forever. With all eight held, the run stops
and emits nothing to diagnose.

`asyncio.wait_for` bounds elapsed time instead, and cancellation frees the slot,
so the failure becomes a logged row naming the stage it died in rather than
silence. The two compose: retries handle calls that fail, deadlines handle calls
that never finish. That is why `profile_app` has both, and why
`DailyQuotaExhausted` is a third category — a wall that neither should be spent
on.

---

## Where I would be guessing

**Why the very first stall showed CPU near zero for fifteen minutes with the
old limiter.** I bounded it and later found the daily cap, which explains the
429 storm and the deadline failures. I did not prove that the *original*
fifteen-minute freeze — the one before any deadline existed — was the same
cause. It is consistent with it, and the ghost-ticket bug is consistent too, but
I never reproduced that specific incident with instrumentation. If asked I
should say the later stall is fully explained and the first one is inferred.

**Whether `min_doc_chars = 250` and `min_browser_chars = 2500` are the right
thresholds.** They were set from two observations — a 133-character Close page
and a roughly 2,000-character Notion stub — not from a distribution over the
corpus. The right defence is that they are floors chosen to exclude two observed
failure modes, not tuned values.

**Whether `api_breadth` is meaningful at three documents per app.** The README
calls it the softest field. I have no measurement to support or refute that
until the human audit scores it per field.
