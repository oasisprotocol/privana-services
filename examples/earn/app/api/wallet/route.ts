import { NextResponse } from "next/server";
import type { Address } from "viem";

import { ERC20_ABI } from "@/lib/abi";
import { baseClient, sapphireClient } from "@/lib/chain";
import { env } from "@/lib/env";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const address = (url.searchParams.get("address") ?? env.testUserAddress) as Address;

  try {
    const base = baseClient();
    const sapphire = sapphireClient();

    const [baseEth, sapphireNative, baseUsdc] = await Promise.all([
      base.getBalance({ address }),
      sapphire.getBalance({ address }),
      base.readContract({
        address: env.aaveUsdc,
        abi: ERC20_ABI,
        functionName: "balanceOf",
        args: [address]
      }) as Promise<bigint>
    ]);

    return NextResponse.json({
      address,
      baseEth: baseEth.toString(),
      sapphireNative: sapphireNative.toString(),
      baseUsdc: baseUsdc.toString()
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
