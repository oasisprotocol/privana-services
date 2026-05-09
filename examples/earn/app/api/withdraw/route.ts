import { NextResponse } from "next/server";
import type { Address, Hex } from "viem";

import { api } from "@/lib/api";
import { signWithdrawConsent } from "@/lib/eip712";
import { env } from "@/lib/env";

export const dynamic = "force-dynamic";

type Body = {
  pool_id: Hex;
  amount: string;
  user_address?: Address;
  // Encrypted SIWE auth token for the withdrawing user. Required: the backend
  // can no longer read withdrawNonces directly, the contract gates the read
  // on a SIWE-recovered caller. Frontend obtains it from accounting's hosted
  // auth via flexvaults-sdk before submitting.
  auth_token: string;
};

export async function POST(req: Request) {
  try {
    const body = (await req.json()) as Body;
    if (!body.pool_id || !body.amount) {
      return NextResponse.json({ error: "pool_id and amount are required" }, { status: 400 });
    }
    if (!body.auth_token) {
      return NextResponse.json({ error: "auth_token is required" }, { status: 400 });
    }
    const userAddress = (body.user_address ?? env.testUserAddress) as Address;

    const { nonce } = await api.withdrawNonce(body.auth_token);

    const signature = await signWithdrawConsent({
      poolId: body.pool_id,
      amount: BigInt(body.amount),
      nonce
    });

    const result = await api.withdraw({
      pool_id: body.pool_id,
      user_address: userAddress,
      amount: body.amount,
      nonce,
      signature
    });
    return NextResponse.json({ result });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
