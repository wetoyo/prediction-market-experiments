# The two-stage matching algorithm

The core problem the UME solves: given a market title from Kalshi and a
market title from Polymarket, decide whether they represent the same
real-world contract. This can't be done with embeddings alone.

## Why embeddings alone aren't enough

A pure vector-similarity approach — embed the whole title, compare cosine
similarity — runs into what the [original design
notes](../../instructions.md) call the "vector blindspot": embedding models
are good at recognizing that two titles are *about* the same topic, but
bad at distinguishing precise numeric strikes or dates within otherwise
similar text. "BTC above $68,000 by Friday" and "BTC above $72,000 by
Friday" would embed as nearly identical, but they are different contracts
with different payouts — collapsing them would be a correctness bug, not
just a false positive.

The fix is to never let the embedding model see or judge the numbers at
all. Instead:

1. An LLM (Groq, `llama-3.1-8b-instant`) extracts structured fields —
   `category`, `underlying_asset`, `condition`, `target_value`, `unit`,
   `resolution_date` — from the free-text title, via
   [`parse_market_title`](../reference/universal-market-embedder.md#parse_market_titletitle-str---marketschema)
   forced into the `MarketSchema` shape by Instructor.
2. Only the `underlying_asset` string (e.g. `"Fed"` vs. `"Federal
   Reserve"`, deliberately just the noun/entity) is embedded and compared
   semantically.
3. The numeric/categorical fields (`target_value`, `condition`,
   `resolution_date`) are compared with exact/near-exact deterministic
   logic, not embeddings.

## Stage 1: soft match (semantic noun similarity)

Implemented in
[`is_soft_match`](../reference/universal-market-embedder.md#matchingpy).
Cosine similarity between the `underlying_asset` embeddings (local
`all-MiniLM-L6-v2` sentence-transformer, no API call) must exceed **0.85**.
This stage answers "are these plausibly about the same entity?" — it's
deliberately permissive, expected to admit some false positives that stage
2 will reject.

## Stage 2: hard match (deterministic validation)

Implemented in
[`is_hard_match`](../reference/universal-market-embedder.md#matchingpy).
All three gates must pass:

1. **Target equivalence** — `target_value_A == target_value_B` exactly.
2. **Directional equivalence** — `condition_A == condition_B` exactly
   (`greater_than`/`less_than`/`equal_to`/`bracket`).
3. **Date proximity** — `abs(resolution_date_A - resolution_date_B) <= 1
   day`, to tolerate timezone-driven settlement-time differences between
   platforms without conflating genuinely different resolution dates.

Both stages must pass — `is_same_event` — for two contracts to be linked
under the same `event_id`.

## The fingerprint short-circuit

Running the full soft+hard scan against every stored event on every new
market is O(n). `build_fingerprint` computes a cheap deterministic string —
`category_sortednouns_condition_target_date` — and checks it against a
`UNIQUE` column first
([`find_matching_event`](../reference/universal-market-embedder.md#find_matching_eventconn-market-marketschema-embedding---int--none)).
An exact fingerprint match is definitionally also a soft+hard match (same
entity nouns, same condition, same target, same date), so this is a safe
shortcut for true duplicates — re-onboarding the same title, or two
platforms using near-identical wording — without changing the matching
semantics. It only helps the exact-string case; genuinely different
wording for the same event ("Fed" vs. "Federal Reserve") still falls
through to the full scan.

## Threshold choices are provisional

`SOFT_MATCH_THRESHOLD = 0.85` and `DATE_PROXIMITY_DAYS = 1` are constants
in `matching.py`, not derived from a labeled dataset — treat them as
starting points to tune once real cross-platform title pairs are
available, not as validated production thresholds.
