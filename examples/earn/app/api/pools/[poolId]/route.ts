import { NextResponse } from "next/server";

import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(_req: Request, ctx: { params: Promise<{ poolId: string }> }) {
  try {
    const { poolId } = await ctx.params;
    const data = await api.pool(poolId);
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
