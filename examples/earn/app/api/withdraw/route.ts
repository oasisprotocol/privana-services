import { NextResponse } from "next/server";
import type { Address } from "viem";

import { api } from "@/lib/api";
import { signWithdrawConsent } from "@/lib/eip712";
import { env } from "@/lib/env";
import {
  ValidationError,
  ensureAddress,
  ensureAmountString,
  ensureHex,
  ensureNonEmptyString,
} from "@/lib/validate";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  try {
    const body = (await req.json()) as Record<string, unknown>;
    const poolId = ensureHex(body.pool_id, "pool_id", 32);
    const amount = ensureAmountString(body.amount, "amount");
    const authToken = ensureNonEmptyString(body.auth_token, "auth_token");
    const userAddress: Address =
      body.user_address === undefined
        ? env.testUserAddress
        : ensureAddress(body.user_address, "user_address");

    const { nonce } = await api.withdrawNonce(authToken);

    const signature = await signWithdrawConsent({
      poolId,
      amount: BigInt(amount),
      nonce
    });

    const result = await api.withdraw({
      pool_id: poolId,
      user_address: userAddress,
      amount,
      nonce,
      signature
    });
    return NextResponse.json({ result });
  } catch (err) {
    const status = err instanceof ValidationError ? 400 : 502;
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status }
    );
  }
}
