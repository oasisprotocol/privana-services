import { NextResponse } from "next/server";

import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  // The backend's /v1/earn/balance is SIWE-gated; the contract recovers the
  // user from the encrypted token and returns only that user's positions.
  // Frontend must obtain a token from accounting's hosted auth (flexvaults-sdk)
  // and pass it through as `token`.
  const url = new URL(req.url);
  const token = url.searchParams.get("token");
  if (!token) {
    return NextResponse.json({ error: "token is required" }, { status: 400 });
  }
  try {
    const data = await api.balance(token);
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
