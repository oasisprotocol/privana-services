import { NextResponse } from "next/server";

import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  // The backend's /v1/earn/balance is SIWE-gated; the contract recovers the
  // user from the encrypted token and returns only that user's positions.
  // The token rides in the X-SIWE-Token header rather than the URL so it
  // doesn't leak into access logs, browser history, or referer chains.
  const token = req.headers.get("x-siwe-token");
  if (!token) {
    return NextResponse.json({ error: "X-SIWE-Token header required" }, { status: 400 });
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
