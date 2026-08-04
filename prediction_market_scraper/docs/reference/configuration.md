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
| `KALSHI_API_KEY_ID` | `Clients.Kalshi.live_datastream`, `live_execution` | Kalshi API key id, sent as the `KALSHI-ACCESS-KEY` header on the websocket handshake and every `live_execution` REST request. |
| `KALSHI_PRIVATE_KEY_PATH` | `Clients.Kalshi.live_datastream`, `live_execution` | Filesystem path to the PEM-encoded RSA private key paired with `KALSHI_API_KEY_ID`, used to sign requests. |
| `POLYMARKET_PRIVATE_KEY` | `Clients.Polymarket.live_execution` | 0x-prefixed wallet private key, used to derive/sign CLOB API credentials and to sign orders (EIP-712). |
| `POLYMARKET_FUNDER` | `Clients.Polymarket.live_execution` (optional) | Wallet address to trade and read positions from, when trading through a Polymarket proxy/Safe wallet (email/Magic login) rather than a raw EOA. |
| `POLYMARKET_SIGNATURE_TYPE` | `Clients.Polymarket.live_execution` (optional) | Signature type for proxy wallets: `1` for email/Magic, `2` for browser-wallet proxy. Omit when trading from a raw EOA. |

No environment variables are required for:
- `Clients.Kalshi.fetch_historical` / `clean_historical` (public REST endpoints)
- `Clients.Polymarket.fetch_historical` / `clean_historical` / `live_datastream` (public REST/websocket endpoints)

`Clients.Polymarket.live_execution` also needs the `py-clob-client` package,
which isn't in any `requirements.txt` yet — see
[Execute live trades](../how-to/execute-live-trades.md).

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
