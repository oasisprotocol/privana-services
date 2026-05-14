import { env } from "./env";

export type PoolSummary = {
  pool_id: string;
  token_id: string;
  strategy: string;
  total_assets: string;
  apy_bps: number;
  status: string;
};

export type PoolDetail = PoolSummary & {
  total_shares: string;
  pool_address: string;
  created_at: number;
};

export type DepositQuote = {
  pool_id: string;
  token_id: string;
  amount: string;
  shares_estimate: string;
  exchange_rate: string;
  pool_address: string;
  transfer_nonce: number;
};

export type Position = {
  pool_id: string;
  token_id: string;
  shares: string;
  underlying_amount: string;
  exchange_rate: string;
};

export type DepositResult = {
  deposit_id: string;
  pool_id: string;
  amount: string;
  shares_minted: string | null;
  exchange_rate: string | null;
  tx_hash: string | null;
  status: string;
};

export type WithdrawResult = {
  withdraw_id: string;
  pool_id: string;
  amount: string;
  shares_burned: string | null;
  exchange_rate: string | null;
  tx_hash: string | null;
  status: string;
};

const parse = async <T>(res: Response): Promise<T> => {
  const text = await res.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { raw: text };
  }
  if (!res.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : res.statusText;
    throw new Error(`${res.status} ${detail}`);
  }
  return body as T;
};

export const api = {
  pools: () =>
    fetch(`${env.apiBaseUrl}/v1/earn/pools`, { cache: "no-store" }).then(
      parse<{ pools: PoolSummary[] }>
    ),

  pool: (poolId: string) =>
    fetch(`${env.apiBaseUrl}/v1/earn/pools/${poolId}`, { cache: "no-store" }).then(
      parse<PoolDetail>
    ),

  balance: (token: string) =>
    fetch(`${env.apiBaseUrl}/v1/earn/balance`, {
      cache: "no-store",
      // SIWE token rides in a header, not the URL: query params end up in
      // server logs, browser history, referer chains, and CDN access logs.
      headers: { "X-SIWE-Token": token }
    }).then(parse<{ positions: Position[] }>),

  quote: (poolId: string, amount: string, userAddress: string) =>
    fetch(
      `${env.apiBaseUrl}/v1/earn/quote?pool_id=${poolId}&amount=${amount}&user_address=${userAddress}`,
      { cache: "no-store" }
    ).then(parse<DepositQuote>),

  deposit: (body: {
    pool_id: string;
    user_address: string;
    amount: string;
    nonce: number;
    signature: string;
  }) =>
    fetch(`${env.apiBaseUrl}/v1/earn/deposit`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body)
    }).then(parse<DepositResult>),

  withdrawNonce: (token: string) =>
    fetch(`${env.apiBaseUrl}/v1/earn/withdraw/nonce`, {
      cache: "no-store",
      headers: { "X-SIWE-Token": token }
    }).then(parse<{ nonce: number }>),

  withdraw: (body: {
    pool_id: string;
    user_address: string;
    amount: string;
    nonce: number;
    signature: string;
  }) =>
    fetch(`${env.apiBaseUrl}/v1/earn/withdraw`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body)
    }).then(parse<WithdrawResult>)
};
