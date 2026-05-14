import { NextResponse } from "next/server";
import { isAddress } from "viem";
import type { Address } from "viem";

import { AAVE_POOL_ABI, ERC20_ABI } from "@/lib/abi";
import { baseClient } from "@/lib/chain";
import { env } from "@/lib/env";

export const dynamic = "force-dynamic";

const RAY_TO_BPS = 10n ** 23n;

export async function GET(req: Request) {
  const url = new URL(req.url);
  const holderRaw = url.searchParams.get("holder") ?? env.lpAddress;
  const assetRaw = url.searchParams.get("asset") ?? env.aaveUsdc;
  if (!isAddress(holderRaw) || !isAddress(assetRaw)) {
    return NextResponse.json({ error: "holder and asset must be valid addresses" }, { status: 400 });
  }
  const holder = holderRaw as Address;
  const asset = assetRaw as Address;

  try {
    const client = baseClient();

    const reserve = (await client.readContract({
      address: env.aavePool,
      abi: AAVE_POOL_ABI,
      functionName: "getReserveData",
      args: [asset]
    })) as { currentLiquidityRate: bigint; aTokenAddress: Address };

    const supplyApyBps = reserve.currentLiquidityRate / RAY_TO_BPS;

    const [aTokenBalance, underlyingBalance, allowance, decimals, symbol] = await Promise.all([
      client.readContract({
        address: reserve.aTokenAddress,
        abi: ERC20_ABI,
        functionName: "balanceOf",
        args: [holder]
      }),
      client.readContract({
        address: asset,
        abi: ERC20_ABI,
        functionName: "balanceOf",
        args: [holder]
      }),
      client.readContract({
        address: asset,
        abi: ERC20_ABI,
        functionName: "allowance",
        args: [holder, env.aavePool]
      }),
      client.readContract({ address: asset, abi: ERC20_ABI, functionName: "decimals" }),
      client.readContract({ address: asset, abi: ERC20_ABI, functionName: "symbol" })
    ]);

    const eth = await client.getBalance({ address: holder });

    return NextResponse.json({
      asset,
      symbol,
      decimals,
      holder,
      pool: env.aavePool,
      aTokenAddress: reserve.aTokenAddress,
      supplyApyBps: supplyApyBps.toString(),
      aTokenBalance: aTokenBalance.toString(),
      underlyingBalance: underlyingBalance.toString(),
      allowance: allowance.toString(),
      ethBalance: eth.toString()
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 502 }
    );
  }
}
