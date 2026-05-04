import { NextResponse } from "next/server";

import { publicEnv } from "@/lib/env";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json(publicEnv());
}
