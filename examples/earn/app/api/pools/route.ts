import { NextResponse } from "next/server";

import { api } from "@/lib/api";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const data = await api.pools();
    return NextResponse.json(data);
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
