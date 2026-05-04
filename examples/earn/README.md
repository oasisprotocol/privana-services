# FlexVaults Earn — local tester UI

Next.js dashboard for poking the earn pipeline against a local backend.
Local-only: signs deposits with the test user key from `.env.local`, talks
to `http://localhost:8001` by default, and reads Aave V3 / Base Sepolia
state via viem.

## Run

```bash
cd examples/earn
bun install
cp .env.local.example .env.local
bun run dev      # http://localhost:3030
```

The Python service must be running on the same `API_BASE_URL` you set in
`.env.local`. From the repo root:

```bash
uv run uvicorn src.main:app --port 8001 --reload
```

## What it shows

- Backend health + key/config presence
- Test user + LP wallets: ETH/USDC on Base Sepolia, native gas on Sapphire
- Earn pools (live `/v1/earn/pools`) with effective AUM
- Selected pool detail: pool_id, token_id, pool address, total shares, on-chain vs aToken drift
- Aave analytics for the LP: supply APY, aToken balance, USDC balance, allowance to pool
- Quote preview, deposit, withdraw forms wired to `/v1/earn/quote|deposit|withdraw`
- Position table per pool for the test user
- Session activity feed with explorer links
- `<FlexvaultsButton>` widget for funding / balance from the official SDK

## Signing

`POST /api/deposit` (Next route handler) fetches a quote, signs an EIP-712
`Transfer(user, pool, tokenId, amount, nonce)` against the AccountingModule
domain using the key from `.env.local`, then forwards the call to
`/v1/earn/deposit`. The browser never sees the key. Withdraws don't need a
user signature, so `POST /api/withdraw` just proxies.

## Stack

- Next.js 16 (App Router, Turbopack)
- shadcn/ui (`base-nova` preset, Tailwind v4)
- wagmi + @tanstack/react-query (peer deps for the SDK)
- `@oasisprotocol/flexvaults-sdk` (provider, hooks, `<FlexvaultsButton>`)
- viem (EIP-712 signing + Sapphire / Base Sepolia reads)

## Caveats

This is a manual tester, not production code. The default `.env.local.example`
ships a known test user private key for the Sapphire testnet flexvaults
demo account. Never reuse it for anything that holds real funds.
