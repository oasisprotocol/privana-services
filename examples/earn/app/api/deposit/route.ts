import { NextResponse } from "next/server";
import type { Address, Hex } from "viem";

import { api } from "@/lib/api";
import { signTransfer } from "@/lib/eip712";
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
    const userAddress = (body.user_address ?? env.testUserAddress) as Address;
    if (!body.pool_id || !body.amount) {
      return NextResponse.json({ error: "pool_id and amount are required" }, { status: 400 });
    }

    const quote = await api.quote(body.pool_id, body.amount, userAddress);

    const signature = await signTransfer({
      userAddress,
      toAddress: quote.pool_address as Address,
      tokenId: quote.token_id as Hex,
      amount: BigInt(body.amount),
      nonce: quote.transfer_nonce
    });

    const result = await api.deposit({
      pool_id: body.pool_id,
      user_address: userAddress,
      amount: body.amount,
      nonce: quote.transfer_nonce,
      signature
    });

    return NextResponse.json({ quote, result });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
