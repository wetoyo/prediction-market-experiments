# Prediction Market Scraper

Tools for pulling market data from Kalshi and Polymarket, normalizing it into
local SQLite databases, and linking equivalent contracts across the two
platforms so they can be traded against each other.

The project has three parts:

- **`Clients/Kalshi`** — REST + websocket client for Kalshi's trade API.
- **`Clients/Polymarket`** — REST + websocket client for Polymarket's Gamma/CLOB APIs.
- **`Clients/Utils/universal_market_embedder`** — the "UME" pipeline that parses
  raw market titles with an LLM, embeds them, and matches equivalent contracts
  across platforms into a shared `events` table.

This documentation follows the [Diátaxis](https://diataxis.fr/) framework, split
into four kinds of document:

## [Tutorials](tutorials/getting-started.md)
Learning-oriented walkthroughs for getting the project running from scratch.

- [Getting started](tutorials/getting-started.md)

## How-to guides
Goal-oriented steps for a specific task you already know you need to do.

- [Fetch and store Kalshi markets](how-to/fetch-kalshi-markets.md)
- [Fetch and store Polymarket markets](how-to/fetch-polymarket-markets.md)
- [Stream live order books](how-to/stream-live-order-books.md)
- [Match markets across platforms with the UME](how-to/run-the-onboarder.md)

## Reference
Information-oriented technical descriptions: function signatures, database
schemas, configuration.

- [Kalshi client reference](reference/kalshi-client.md)
- [Polymarket client reference](reference/polymarket-client.md)
- [Universal Market Embedder reference](reference/universal-market-embedder.md)
- [Database schemas](reference/database-schemas.md)
- [Configuration and environment variables](reference/configuration.md)

## Explanation
Understanding-oriented discussion of design and rationale.

- [System architecture: onboarding vs. execution loop](explanation/architecture.md)
- [The two-stage matching algorithm](explanation/matching-algorithm.md)
