import { NextResponse } from "next/server";

import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const userAddress = url.searchParams.get("user_address");
  if (!userAddress) {
    return NextResponse.json({ error: "user_address is required" }, { status: 400 });
  }
  try {
    const data = await api.balance(userAddress);
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
