import { NextResponse } from "next/server";

import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const poolId = url.searchParams.get("pool_id");
  const amount = url.searchParams.get("amount");
  const userAddress = url.searchParams.get("user_address");
  if (!poolId || !amount || !userAddress) {
    return NextResponse.json(
      { error: "pool_id, amount and user_address are required" },
      { status: 400 }
    );
  }
  try {
    const data = await api.quote(poolId, amount, userAddress);
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
