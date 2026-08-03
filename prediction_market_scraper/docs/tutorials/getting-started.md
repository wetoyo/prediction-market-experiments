# Getting started

This tutorial takes you from a clean checkout to fetching real markets from
both Kalshi and Polymarket, storing them in local SQLite databases, and
running the matcher that links equivalent contracts across the two
platforms.

## 1. Create a virtual environment

From the project root:

```bash
python -m venv venv
./venv/Scripts/python.exe -m pip install --upgrade pip
```

## 2. Install dependencies

The Kalshi and Polymarket clients need `requests` and `websockets`. The
matching pipeline (UME) needs the packages listed in its
`requirements.txt`:

```bash
./venv/Scripts/pip.exe install requests websockets
./venv/Scripts/pip.exe install -r Clients/Utils/universal_market_embedder/requirements.txt
```

This pulls in `groq`, `instructor`, `pydantic`, `sentence-transformers`,
`numpy`, and `python-dotenv`. `sentence-transformers` will download the
`all-MiniLM-L6-v2` model the first time it runs.

## 3. Set your Groq API key

The onboarding pipeline uses [Groq](https://groq.com/) to parse market
titles into structured data. Create a `.env` file in the project root:

```
GROQ_API_KEY=your-groq-key-here
```

`onboarder.py` loads this automatically via `python-dotenv`. See
[Configuration](../reference/configuration.md) for the full list of
environment variables, including the ones needed for Kalshi's websocket.

## 4. Fetch some markets

Fetch Kalshi's Federal Reserve rate-decision series and store it in
`Data/kalshi.db`:

```bash
./venv/Scripts/python.exe -m Clients.Kalshi.clean_historical
```

Fetch a Polymarket search result for "bitcoin price" and store it in
`Data/polymarket.db`:

```bash
./venv/Scripts/python.exe -m Clients.Polymarket.clean_historical
```

Both commands print the number of rows written. Details on filtering by
series, category, or search query are in the how-to guides:
[Fetch and store Kalshi markets](../how-to/fetch-kalshi-markets.md) and
[Fetch and store Polymarket markets](../how-to/fetch-polymarket-markets.md).

## 5. Match a market across platforms

Run the onboarder's demo, which parses one Kalshi title and one Polymarket
title describing the same Fed rate hike, embeds their underlying assets,
and checks whether they resolve to the same `event_id`:

```bash
./venv/Scripts/python.exe -m Clients.Utils.universal_market_embedder.onboarder
```

You should see both markets parsed into structured JSON, followed by
`Matched: both contracts linked under event_id=...`. The row lands in
`Data/market_embedder.db`.

## Next steps

- Read [System architecture](../explanation/architecture.md) to understand
  why fetching/storing and matching are split into separate loops.
- Read [The two-stage matching algorithm](../explanation/matching-algorithm.md)
  to understand how `event_id` matches are decided.
- To stream live order books instead of historical snapshots, see
  [Stream live order books](../how-to/stream-live-order-books.md).
