import { NextResponse } from "next/server";

import { env } from "@/lib/env";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const res = await fetch(`${env.apiBaseUrl}/health`, { cache: "no-store" });
    return NextResponse.json({ ok: res.ok, status: res.status });
  } catch (err) {
    return NextResponse.json(
      { ok: false, status: 0, error: err instanceof Error ? err.message : String(err) },
      { status: 200 }
    );
  }
}
