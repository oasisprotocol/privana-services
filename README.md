# FlexVaults Swap

Order routing service for FlexVaults token swaps. Fetches rates from LiFi, applies a configurable fee, and executes atomic on-chain swaps via the SwapManager contract on Oasis Sapphire.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Copy the example env and fill in your values:

```bash
cp .env.testnet .env
```

Required variables:

| Variable | Description |
|----------|-------------|
| `LIQUIDITY_PROVIDER_PRIVATE_KEY` | LP wallet private key |
| `LIQUIDITY_PROVIDER_ADDRESS` | LP wallet address |
| `ACCOUNTING_CONTRACT_ADDRESS` | Accounting proxy contract |
| `SWAP_MANAGER_CONTRACT_ADDRESS` | SwapManager contract |
| `LIFI_API_KEY` | LiFi API key (optional, improves rate limits) |

## Run

```bash
uv run flexvaults-swap
```

The API starts on `http://localhost:8000` by default. Configure with `API_HOST` and `API_PORT`.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/v1/quote` | Get a swap quote |
| `POST` | `/v1/swap` | Execute a swap |
| `GET` | `/v1/swap/{id}/status` | Check swap status |
| `GET` | `/v1/tokens` | List supported tokens |
| `GET` | `/v1/chains` | List supported chains |

## Tests

Unit tests (no network or credentials required):

```bash
uv run pytest tests/ -m "not integration"
```

Integration tests (requires `.env` with LP credentials, hits live testnet):

```bash
uv run pytest tests/integration/ -v -s
```

All tests:

```bash
uv run pytest tests/ -v
```

## Solidity

SwapManager contract and tests are in `solidity/`. Requires Node.js:

```bash
cd solidity
bun install
bun run test
```
