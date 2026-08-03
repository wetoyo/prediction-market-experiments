# System architecture: onboarding vs. execution loop

The project splits work into two pipelines with very different latency and
cost profiles, so that expensive work (LLM calls, semantic matching) never
sits on the critical path of time-sensitive work (reading live order-book
deltas).

```
                    [ Onboarding Loop (Slow Brain) ]
                                   |
                Ingests new contracts from Kalshi/Polymarket
                                   |
                Extracts variables using Groq + Instructor
                                   |
                 Resolves entities & calculates similarity
                                   |
                       Writes to SQLite cache
                                   |
                                   v
                    [ Execution Loop (Fast Brain) ]
                                   |
                 Streams live order books via websockets
                                   |
                     Queries SQLite for matched IDs
                                   |
                Executes high-speed arbitrage decisions
```

## Why split it this way

Linking "Will the Fed raise rates 25bps in September?" (Kalshi) to "Fed
interest rate hike 25bps in Sept?" (Polymarket) requires understanding
natural language — an LLM call plus a semantic embedding comparison. That's
slow (network round-trip to Groq) and rate-limited (Groq's free tier: 30
requests/minute, 14,400/day). None of that can happen inline while reading
a live order-book feed, where the useful window for a price discrepancy is
seconds.

So the two concerns are separated by module:

- **Onboarding** (`Clients.Utils.universal_market_embedder`): runs
  offline/periodically. Takes a raw title, calls Groq once to extract
  structured terms (`MarketSchema`), embeds the entity locally, and decides
  whether it matches an existing event or is a new one. Writes the result —
  a link between a platform-specific market id and a shared `event_id` — to
  `Data/market_embedder.db`. See
  [The two-stage matching algorithm](matching-algorithm.md) for how the
  match decision itself works.

- **Execution** (`Clients.Kalshi.live_datastream`,
  `Clients.Polymarket.live_datastream`): runs continuously. Streams
  order-book deltas over websockets for a known list of market/asset ids.
  Does no parsing, no embedding, no LLM calls — just reads
  `Data/market_embedder.db` for which ids are linked and reacts to price
  changes.

## Why separate fetch/clean from onboarding

`Clients.Kalshi` and `Clients.Polymarket` each write their own SQLite
database (`kalshi.db`, `polymarket.db`) independent of the UME. This keeps
the platform clients simple — REST paging and upserts, no LLM dependency —
and means raw market data can be collected even if Groq is unavailable or
rate-limited. The UME reads `raw_title` values produced by these steps and
does its own separate write into `market_embedder.db`
([see how-to](../how-to/run-the-onboarder.md)); it does not modify
`kalshi.db`/`polymarket.db`.

## Current state of the execution loop

As of this writing, `Clients.Polymarket.live_datastream` is usable (the
CLOB market channel is public, no credentials needed).
`Clients.Kalshi.live_datastream` is implemented — including the RSA-PSS
request signing every Kalshi websocket subscription requires — but
untested against the live socket, since Kalshi API credentials
(`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`) aren't set up in this
project yet. There is no code yet that reads `market_embedder.db` to drive
the execution loop's subscriptions or make trading decisions — the
"Executes high-speed arbitrage decisions" step in the diagram above is not
implemented.
