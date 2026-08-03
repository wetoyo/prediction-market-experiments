# Derbit Documentation

`derbit` is a small toolkit for pulling market data from [Deribit](https://www.deribit.com)'s public v2 API and persisting it locally. It has three parts:

- **`fetch_historical.py`** — a synchronous REST client for Deribit's public endpoints (instruments, order books, tickers, trades, volatility, etc.).
- **`clean_historical.py`** — normalizes raw order book payloads and upserts them into a local SQLite database (`Data/derbit.db`).
- **`live_datastream.py`** — an async WebSocket client for subscribing to live order book, ticker, and trade updates.

This documentation is organized using the [Diátaxis](https://diataxis.fr) framework, so you can jump to the kind of material that matches what you're trying to do:

| If you want to... | Go to |
|---|---|
| Follow a hands-on lesson to get something working end to end | [Tutorials](tutorials/getting-started.md) |
| Accomplish a specific task you already understand | [How-to guides](how-to-guides/) |
| Look up a function signature, parameter, or the DB schema | [Reference](reference/) |
| Understand why the module is built the way it is | [Explanation](explanation/architecture.md) |

## Contents

- [Tutorials](tutorials/getting-started.md)
  - [Getting started with derbit](tutorials/getting-started.md)
- [How-to guides](how-to-guides/)
  - [Fetch and store order books for a currency](how-to-guides/fetch-and-store-order-books.md)
  - [Stream live updates over WebSocket](how-to-guides/stream-live-updates.md)
  - [Query the local database](how-to-guides/query-the-database.md)
- [Reference](reference/)
  - [`fetch_historical` — REST client](reference/fetch_historical.md)
  - [`clean_historical` — database layer](reference/clean_historical.md)
  - [`live_datastream` — WebSocket client](reference/live_datastream.md)
- [Explanation](explanation/architecture.md)
  - [Architecture and design decisions](explanation/architecture.md)
