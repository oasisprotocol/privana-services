# FlexVaults Swap

Order routing service for FlexVaults. Two pipelines:

- **Swap** — fetches rates from LiFi, applies a configurable fee, and executes atomic on-chain swaps via the `SwapManager` contract on Oasis Sapphire.
- **Earn** — registers yield strategies behind the `EarnManager` contract on Sapphire. Pluggable per-pool adapters: **Aave V3** on Base Sepolia (variable-rate lending) and **Midas mTBILL** on Base mainnet (tokenized US Treasury bills). Deposits bridge accounting funds to Base, deploy into the configured protocol, and mint pool shares; withdrawals redeem and bridge back.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Copy the example env and fill in your values:

```bash
cp .env.testnet .env
```

### Required variables

| Variable | Description |
|----------|-------------|
| `LIQUIDITY_PROVIDER_SECRET_KEY` | LP wallet secret key (signs accounting transfers and Aave bridges; LP/pool address is derived from this). The legacy `LIQUIDITY_PROVIDER_PRIVATE_KEY` env name is still accepted with a deprecation warning. |
| `ACCOUNTING_CONTRACT_ADDRESS` | Accounting proxy on Sapphire |
| `ACCOUNTING_CHAIN_ID` | Chain id for EIP-712 domain (Sapphire testnet = `23295`) |
| `ACCOUNTING_API_BASE_URL` | Accounting REST API (e.g. `https://flexvaults-staging.rofl.build`) |
| `SWAP_MANAGER_CONTRACT_ADDRESS` | `SwapManager` contract on Sapphire |
| `EARN_MANAGER_CONTRACT_ADDRESS` | `EarnManager` proxy contract on Sapphire |
| `SAPPHIRE_RPC_URL` | Sapphire RPC endpoint |
| `BASE_SEPOLIA_RPC_URL` | Base Sepolia RPC endpoint (for Aave reads/writes) |
| `AAVE_POOL_ADDRESS` | Aave V3 `Pool` on Base Sepolia |
| `AAVE_POOL_ASSETS` | JSON map of `pool_id -> {token_id, asset_address}` registering Aave strategies at startup |
| `BASE_MAINNET_RPC_URL` | Base mainnet RPC endpoint (for Midas reads/writes) |
| `MIDAS_ISSUANCE_VAULT_ADDRESS` | Midas Issuance Vault proxy on Base mainnet (defaults to the canonical deployment) |
| `MIDAS_REDEMPTION_VAULT_ADDRESS` | Midas Instant Redemption Vault proxy on Base mainnet |
| `MIDAS_MTBILL_TOKEN_ADDRESS` | mTBILL ERC20 on Base mainnet |
| `MIDAS_ORACLE_ADDRESS` | Chronicle MTBILL/USD price oracle on Base mainnet |
| `MIDAS_DEFAULT_SLIPPAGE_BPS` | Slippage tolerance on `depositInstant` / `redeemInstant` (default 50 = 0.5%) |
| `MIDAS_ORACLE_HEARTBEAT_SEC` | Max oracle staleness before `is_healthy()` refuses routing (default 86400 = 24h; checked against 2× this) |
| `MIDAS_APY_BPS` | Admin-managed display APY for Midas pools (default 350 = 3.5%); display only, not routing |
| `MIDAS_POOL_ASSETS` | JSON map of `pool_id -> token_id` registering MidasStrategy per pool at startup |
| `LIFI_API_KEY` | LiFi API key (optional, improves rate limits) |
| `LIFI_API_URL` | LiFi base URL (defaults to `https://li.quest/v1`) |
| `LIFI_INTEGRATOR` | LiFi integrator id |
| `LIFI_TOKEN_MAP` | JSON map for testnet→mainnet pricing fallbacks |
| `SUPPORTED_TOKEN_IDS` | Comma-separated accounting token ids |
| `SUPPORTED_CHAINS` | JSON array of supported chains |
| `FEE_BPS` | Swap fee in basis points (e.g. `150` = 1.5%) |
| `QUOTE_TTL` | Quote validity window in seconds |
| `MAX_SWAP_AMOUNT_USD` | Per-swap cap |
| `API_HOST` / `API_PORT` | Server bind (defaults `0.0.0.0:8000`) |
| `LOG_LEVEL` | Log level (default `INFO`) |
| `ENVIRONMENT` | `development` / `staging` / `production` |
| `ADMIN_API_KEY` | Optional admin auth for management routes |

## Deployed Addresses

### Sapphire Testnet (chainId 23295)

| Contract | Address |
|----------|---------|
| SwapManager | `0x6a0a11Aa78c575e6C9CFD295104F36b3964991BC` |
| EarnManager | `0x96e8fFdb9432f2A56CDeF0F9834E10A47ea029F9` |
| Accounting (proxy) | `0xad3C76e4E621C0cfF7540479Ee9B0A945723A642` |

### Earn Pools

| Pool | poolId | tokenId | poolAddress | Strategy |
|------|--------|---------|-------------|----------|
| Aave USDC (Base Sepolia) | `0xeeed5d5fb4fdf07abc1f232dc05d0cd551bae3a1c9a83dc1cbd196893afedd29` | `0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514` | `0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c` | aave-v3 |

### Base Sepolia (chainId 84532)

| Contract | Address |
|----------|---------|
| Aave V3 Pool | `0x07eA79F68B2B3df564D0A34F8e19D9B1e339814b` |
| USDC | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` |

### Base Mainnet (chainId 8453)

| Contract | Address |
|----------|---------|
| Midas Issuance Vault | `0x8978e327FE7C72Fa4eaF4649C23147E279ae1470` |
| Midas Instant Redemption Vault | `0x2a8c22E3b10036f3AEF5875d04f8441d4188b656` |
| mTBILL | `0xDD629E5241CbC5919847783e6C96B2De4754e438` |
| Chronicle MTBILL/USD Oracle | `0x70E58b7A1c884fFFE7dbce5249337603a28b8422` |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |

## Run

```bash
uv run flexvaults-swap
```

The API starts on `http://localhost:8000` by default. Configure with `API_HOST` and `API_PORT`.

## API Endpoints

### Common

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/v1/tokens` | List supported tokens |
| `GET` | `/v1/chains` | List supported chains |

### Swap

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/quote` | Quote a swap (LiFi rate + fee, returns LP transfer nonce) |
| `POST` | `/v1/swap` | Execute the atomic dual-transfer through `SwapManager` on Sapphire |
| `GET` | `/v1/swap/{swap_id}/status` | Status for a previously submitted swap |

### Earn

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/v1/earn/pools` | none | List registered Earn pools (with live strategy AUM) |
| `GET` | `/v1/earn/pools/{pool_id}` | none | Pool detail, including current APY and assets |
| `GET` | `/v1/earn/quote` | none | Deposit quote: shares estimate, exchange rate, transfer nonce |
| `POST` | `/v1/earn/deposit` | EIP-712 signature | Bridge funds to Base, supply to Aave, mint pool shares |
| `GET` | `/v1/earn/withdraw/nonce` | `Authorization: Bearer <Privana JWT>` | Withdraw consent nonce for the authenticated user |
| `POST` | `/v1/earn/withdraw` | EIP-712 signature | Burn shares, redeem from Aave, bridge back to accounting |
| `GET` | `/v1/earn/balance` | `Authorization: Bearer <Privana JWT>` | User shares + redeemable assets across pools |

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

`SwapManager` and `EarnManager` contracts plus tests live in `solidity/`. Requires Node.js (uses `bun`):

```bash
cd solidity
bun install
bun run test
```

Pool registration script (one-shot, calls `EarnManager.createPool` against the deployed manager — already executed on Sapphire testnet):

```bash
cd solidity
bun run hardhat run scripts/create-aave-usdc-pool.ts --network sapphire-testnet
```
