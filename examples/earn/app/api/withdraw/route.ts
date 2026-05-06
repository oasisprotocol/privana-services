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
};

export async function POST(req: Request) {
  try {
    const body = (await req.json()) as Body;
    if (!body.pool_id || !body.amount) {
      return NextResponse.json({ error: "pool_id and amount are required" }, { status: 400 });
    }
    const userAddress = (body.user_address ?? env.testUserAddress) as Address;

    // Fetch the user's current EarnManager.withdrawNonces[user] and bind it
    // into the EIP-712 consent message. Server side then verifies on-chain.
    const { nonce } = await api.withdrawNonce(userAddress);

    const signature = await signWithdrawConsent({
      user: userAddress,
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
