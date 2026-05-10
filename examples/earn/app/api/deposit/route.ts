import { NextResponse } from "next/server";
import type { Address, Hex } from "viem";

import { api } from "@/lib/api";
import { signTransfer } from "@/lib/eip712";
import { env } from "@/lib/env";
import {
  ValidationError,
  ensureAddress,
  ensureAmountString,
  ensureHex,
} from "@/lib/validate";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  try {
    const body = (await req.json()) as Record<string, unknown>;
    const poolId = ensureHex(body.pool_id, "pool_id", 32);
    const amount = ensureAmountString(body.amount, "amount");
    const userAddress: Address =
      body.user_address === undefined
        ? env.testUserAddress
        : ensureAddress(body.user_address, "user_address");

    const quote = await api.quote(poolId, amount, userAddress);

    const poolAddress = ensureAddress(quote.pool_address, "quote.pool_address");
    const tokenId = ensureHex(quote.token_id, "quote.token_id", 32);

    const signature = await signTransfer({
      userAddress,
      toAddress: poolAddress,
      tokenId: tokenId as Hex,
      amount: BigInt(amount),
      nonce: quote.transfer_nonce
    });

    const result = await api.deposit({
      pool_id: poolId,
      user_address: userAddress,
      amount,
      nonce: quote.transfer_nonce,
      signature
    });

    return NextResponse.json({ quote, result });
  } catch (err) {
    const status = err instanceof ValidationError ? 400 : 502;
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status }
    );
  }
}
