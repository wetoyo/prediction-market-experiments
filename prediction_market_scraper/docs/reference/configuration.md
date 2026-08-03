# Configuration and environment variables

## `.env` file

`Clients.Utils.universal_market_embedder.onboarder` calls
`load_dotenv()` on import, so a `.env` file in the project root is picked
up automatically for anything importing that package. The Kalshi/Polymarket
clients don't load `.env` themselves — export `KALSHI_*` variables in your
shell environment, or load them yourself before importing
`Clients.Kalshi.live_datastream`.

## Environment variables

| Variable | Required by | Purpose |
|---|---|---|
| `GROQ_API_KEY` | `universal_market_embedder.onboarder` | Auth for the Groq API, used to parse market titles into `MarketSchema` via Instructor. |
| `KALSHI_API_KEY_ID` | `Clients.Kalshi.live_datastream` | Kalshi API key id, sent as the `KALSHI-ACCESS-KEY` header on the websocket handshake. |
| `KALSHI_PRIVATE_KEY_PATH` | `Clients.Kalshi.live_datastream` | Filesystem path to the PEM-encoded RSA private key paired with `KALSHI_API_KEY_ID`, used to sign the handshake. |

No environment variables are required for:
- `Clients.Kalshi.fetch_historical` / `clean_historical` (public REST endpoints)
- `Clients.Polymarket.fetch_historical` / `clean_historical` / `live_datastream` (public REST/websocket endpoints)

## Data paths

Database paths are computed relative to each module's file location and
default to a sibling `Data/` directory at the project root — they don't
need configuring, but can be overridden per-call:

| Module | Default path | Override |
|---|---|---|
| `Clients.Kalshi.clean_historical` | `Data/kalshi.db` | `get_connection(db_path=...)` |
| `Clients.Polymarket.clean_historical` | `Data/polymarket.db` | `get_connection(db_path=...)` |
| `Clients.Utils.universal_market_embedder.database` | `Data/market_embedder.db` | `get_connection(db_path=...)` |

All three call `db_path.parent.mkdir(parents=True, exist_ok=True)`, so the
`Data/` directory is created automatically on first use.
