"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { FlexvaultsButton } from "@oasisprotocol/flexvaults-sdk";
import { toast } from "sonner";
import { useAccount, useConnect, useDisconnect } from "wagmi";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle
} from "@/components/ui/card";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle
} from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from "@/components/ui/input-group";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { explorerAddr, explorerTx } from "@/lib/explorers";
import { fmtBaseUnits, shortAddr, shortHash } from "@/lib/format";
import { cn } from "@/lib/utils";

type ConfigPayload = {
  apiBaseUrl: string;
  testUserAddress: string;
  lpAddress: string;
  accountingContract: string;
  accountingChainId: number;
  earnManagerContract: string;
  sapphireRpc: string;
  baseSepoliaRpc: string;
  aavePool: string;
  aaveUsdc: string;
  defaultPoolId: string;
  defaultUsdcTokenId: string;
  hasUserKey: boolean;
};

type Pool = {
  pool_id: string;
  token_id: string;
  strategy: string;
  total_assets: string;
  apy_bps: number;
  status: string;
};

type PoolDetail = Pool & {
  total_shares: string;
  pool_address: string;
  created_at: number;
};

type Position = {
  pool_id: string;
  token_id: string;
  shares: string;
  underlying_amount: string;
  exchange_rate: string;
};

type Quote = {
  pool_id: string;
  token_id: string;
  amount: string;
  shares_estimate: string;
  exchange_rate: string;
  pool_address: string;
  transfer_nonce: number;
};

type DepositResult = {
  deposit_id: string;
  pool_id: string;
  amount: string;
  shares_minted: string | null;
  exchange_rate: string | null;
  tx_hash: string | null;
  status: string;
};

type WithdrawResult = {
  withdraw_id: string;
  pool_id: string;
  amount: string;
  shares_burned: string | null;
  exchange_rate: string | null;
  tx_hash: string | null;
  status: string;
};

type Aave = {
  asset: string;
  symbol: string;
  decimals: number;
  holder: string;
  pool: string;
  aTokenAddress: string;
  supplyApyBps: string;
  aTokenBalance: string;
  underlyingBalance: string;
  allowance: string;
  ethBalance: string;
};

type Wallet = {
  address: string;
  baseEth: string;
  sapphireNative: string;
  baseUsdc: string;
};

type ActivityKind = "deposit" | "withdraw" | "error";
type ChainId = "base-sepolia" | "sapphire-testnet";
type ActivityEntry = {
  ts: number;
  kind: ActivityKind;
  label: string;
  txHash?: string | null;
  chain?: ChainId;
  detail?: string;
};

const POLL_MS = 6000;
const USDC_DECIMALS = 6;

const fetchJSON = async <T,>(url: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(url, { cache: "no-store", ...init });
  const text = await res.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { raw: text };
  }
  if (!res.ok) {
    const detail =
      typeof body === "object" && body !== null && "error" in body
        ? String((body as { error: unknown }).error)
        : res.statusText;
    throw new Error(`${res.status} ${detail}`);
  }
  return body as T;
};

export default function Page() {
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [health, setHealth] = useState<{ ok: boolean; status: number; error?: string } | null>(null);
  const [pools, setPools] = useState<Pool[]>([]);
  const [poolDetail, setPoolDetail] = useState<PoolDetail | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [aave, setAave] = useState<Aave | null>(null);
  const [userWallet, setUserWallet] = useState<Wallet | null>(null);
  const [lpWallet, setLpWallet] = useState<Wallet | null>(null);

  const [selectedPoolId, setSelectedPoolId] = useState<string>("");
  const [amountHuman, setAmountHuman] = useState<string>("1");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [pendingAction, setPendingAction] = useState<null | "deposit" | "withdraw" | "quote">(null);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState<boolean>(false);

  useEffect(() => {
    fetchJSON<ConfigPayload>("/api/config")
      .then(setConfig)
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (config && !selectedPoolId && config.defaultPoolId) {
      setSelectedPoolId(config.defaultPoolId);
    }
  }, [config, selectedPoolId]);

  const refreshAll = useCallback(async () => {
    if (!config) return;
    setRefreshing(true);
    try {
      const [healthRes, poolsRes, balanceRes, aaveRes, userRes, lpRes] = await Promise.all([
        fetchJSON<{ ok: boolean; status: number; error?: string }>("/api/health"),
        fetchJSON<{ pools: Pool[] }>("/api/pools"),
        fetchJSON<{ positions: Position[] }>(`/api/balance?user_address=${config.testUserAddress}`),
        fetchJSON<Aave>(`/api/aave?holder=${config.lpAddress}&asset=${config.aaveUsdc}`),
        fetchJSON<Wallet>(`/api/wallet?address=${config.testUserAddress}`),
        fetchJSON<Wallet>(`/api/wallet?address=${config.lpAddress}`)
      ]);
      setHealth(healthRes);
      setPools(poolsRes.pools);
      setPositions(balanceRes.positions);
      setAave(aaveRes);
      setUserWallet(userRes);
      setLpWallet(lpRes);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  }, [config]);

  useEffect(() => {
    if (!config) return;
    refreshAll();
    const id = window.setInterval(refreshAll, POLL_MS);
    return () => window.clearInterval(id);
  }, [config, refreshAll]);

  useEffect(() => {
    if (!selectedPoolId) return;
    fetchJSON<PoolDetail>(`/api/pools/${selectedPoolId}`)
      .then(setPoolDetail)
      .catch(() => setPoolDetail(null));
  }, [selectedPoolId, pools]);

  const amountBaseUnits = useMemo(() => {
    if (!amountHuman) return "";
    const trimmed = amountHuman.trim();
    if (!/^\d+(\.\d+)?$/.test(trimmed)) return "";
    const [whole, frac = ""] = trimmed.split(".");
    const padded = (frac + "0".repeat(USDC_DECIMALS)).slice(0, USDC_DECIMALS);
    return BigInt(whole + padded).toString();
  }, [amountHuman]);

  const previewQuote = useCallback(async () => {
    if (!config || !selectedPoolId || !amountBaseUnits) return;
    setPendingAction("quote");
    try {
      const q = await fetchJSON<Quote>(
        `/api/quote?pool_id=${selectedPoolId}&amount=${amountBaseUnits}&user_address=${config.testUserAddress}`
      );
      setQuote(q);
      toast.success("Quote refreshed", { description: `${q.shares_estimate} shares · rate ${q.exchange_rate}` });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error("Quote failed", { description: msg });
    } finally {
      setPendingAction(null);
    }
  }, [config, selectedPoolId, amountBaseUnits]);

  const submitDeposit = useCallback(async () => {
    if (!config || !selectedPoolId || !amountBaseUnits) return;
    setPendingAction("deposit");
    try {
      const { result } = await fetchJSON<{ result: DepositResult; quote: Quote }>("/api/deposit", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ pool_id: selectedPoolId, amount: amountBaseUnits })
      });
      setActivity((prev) => [
        {
          ts: Date.now(),
          kind: "deposit",
          label: `Deposit ${amountHuman} USDC into ${shortHash(selectedPoolId)}`,
          txHash: result.tx_hash,
          chain: "sapphire-testnet",
          detail:
            result.shares_minted !== null
              ? `+${result.shares_minted} shares · rate ${result.exchange_rate}`
              : `status=${result.status}`
        },
        ...prev
      ]);
      toast.success("Deposit confirmed", {
        description: result.tx_hash ? shortHash(result.tx_hash) : `status=${result.status}`
      });
      await refreshAll();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setActivity((prev) => [
        { ts: Date.now(), kind: "error", label: "Deposit failed", detail: msg },
        ...prev
      ]);
      toast.error("Deposit failed", { description: msg });
    } finally {
      setPendingAction(null);
    }
  }, [config, selectedPoolId, amountBaseUnits, amountHuman, refreshAll]);

  const submitWithdraw = useCallback(async () => {
    if (!config || !selectedPoolId || !amountBaseUnits) return;
    setPendingAction("withdraw");
    try {
      const { result } = await fetchJSON<{ result: WithdrawResult }>("/api/withdraw", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ pool_id: selectedPoolId, amount: amountBaseUnits })
      });
      setActivity((prev) => [
        {
          ts: Date.now(),
          kind: "withdraw",
          label: `Withdraw ${amountHuman} USDC from ${shortHash(selectedPoolId)}`,
          txHash: result.tx_hash,
          chain: "sapphire-testnet",
          detail:
            result.shares_burned !== null
              ? `-${result.shares_burned} shares · rate ${result.exchange_rate}`
              : `status=${result.status}`
        },
        ...prev
      ]);
      toast.success("Withdraw confirmed", {
        description: result.tx_hash ? shortHash(result.tx_hash) : `status=${result.status}`
      });
      await refreshAll();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setActivity((prev) => [
        { ts: Date.now(), kind: "error", label: "Withdraw failed", detail: msg },
        ...prev
      ]);
      toast.error("Withdraw failed", { description: msg });
    } finally {
      setPendingAction(null);
    }
  }, [config, selectedPoolId, amountBaseUnits, amountHuman, refreshAll]);

  if (!config) {
    return (
      <main className="mx-auto flex min-h-screen w-full max-w-[1600px] items-center justify-center px-6 py-10">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner className="size-4" /> Loading config…
        </div>
      </main>
    );
  }

  const positionForSelected = positions.find((p) => p.pool_id === selectedPoolId);

  return (
    <main className="flex w-full flex-col gap-6 px-8 py-10 xl:px-12 2xl:px-16">
      <Header config={config} health={health} refreshing={refreshing} onRefresh={refreshAll} />

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Backend / RPC error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <section className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <WalletCard
          title="Test user"
          chainNote="Base Sepolia (gas) · Sapphire (gas)"
          address={config.testUserAddress}
          wallet={userWallet}
        />
        <WalletCard
          title="Liquidity pool (LP) EOA"
          chainNote="Base Sepolia (gas + USDC float) · Sapphire (gas)"
          address={config.lpAddress}
          wallet={lpWallet}
        />
      </section>

      <section className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <PoolListCard pools={pools} selectedPoolId={selectedPoolId} onSelect={setSelectedPoolId} />
        <PoolDetailCard pool={poolDetail} aave={aave} />
        <AaveCard aave={aave} hasPool={pools.length > 0} />
      </section>

      <section className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <ActionCard
          poolId={selectedPoolId}
          amountHuman={amountHuman}
          onAmountChange={setAmountHuman}
          amountBaseUnits={amountBaseUnits}
          quote={quote}
          onPreview={previewQuote}
          onDeposit={submitDeposit}
          onWithdraw={submitWithdraw}
          pendingAction={pendingAction}
          position={positionForSelected}
          hasUserKey={config.hasUserKey}
        />
        <ActivityCard entries={activity} />
        <PositionsCard positions={positions} />
      </section>
    </main>
  );
}

function Header({
  config,
  health,
  refreshing,
  onRefresh
}: {
  config: ConfigPayload;
  health: { ok: boolean; status: number; error?: string } | null;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const tone = !health ? "warming" : health.ok ? "online" : "offline";
  const variant: "secondary" | "outline" | "destructive" =
    !health ? "secondary" : health.ok ? "outline" : "destructive";
  return (
    <header className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
      <div className="flex flex-col gap-1.5">
        <h1 className="text-3xl font-semibold tracking-tight">FlexVaults Earn</h1>
        <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <span>{config.apiBaseUrl}</span>
          <Separator orientation="vertical" className="h-3.5" />
          <span>Sapphire chain id {config.accountingChainId}</span>
          <Separator orientation="vertical" className="h-3.5" />
          <span>Aave V3 · Base Sepolia</span>
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={variant}>
          backend {tone}
          {health && !health.ok && health.error ? <span className="text-muted-foreground">· {health.error}</span> : null}
        </Badge>
        <Badge variant={config.hasUserKey ? "outline" : "destructive"}>
          {config.hasUserKey ? "user key loaded" : "no user key"}
        </Badge>
        <WalletConnect />
        <FlexvaultsLauncher />
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? <Spinner data-icon="inline-start" /> : null}
          Refresh
        </Button>
      </div>
    </header>
  );
}

function WalletCard({
  title,
  chainNote,
  address,
  wallet
}: {
  title: string;
  chainNote: string;
  address: string;
  wallet: Wallet | null;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{chainNote}</CardDescription>
        <CardAction>
          <Tooltip>
            <TooltipTrigger
              render={
                <Button
                  variant="ghost"
                  size="sm"
                  nativeButton={false}
                  render={
                    <a
                      href={explorerAddr(address, "base-sepolia")}
                      target="_blank"
                      rel="noreferrer"
                    />
                  }
                >
                  Basescan
                </Button>
              }
            />
            <TooltipContent>{address}</TooltipContent>
          </Tooltip>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Stat label="address" value={<span className="font-mono">{shortAddr(address)}</span>} hint={address} />
        <Separator />
        <div className="grid grid-cols-3 gap-4">
          <Stat label="ETH (base)" value={wallet ? fmtBaseUnits(wallet.baseEth, 18) : <Skeleton className="h-5 w-20" />} />
          <Stat label="USDC (base)" value={wallet ? fmtBaseUnits(wallet.baseUsdc, USDC_DECIMALS) : <Skeleton className="h-5 w-20" />} />
          <Stat label="Native (sapphire)" value={wallet ? fmtBaseUnits(wallet.sapphireNative, 18) : <Skeleton className="h-5 w-20" />} />
        </div>
      </CardContent>
    </Card>
  );
}

function Stat({
  label,
  value,
  hint
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span className="font-mono text-sm break-all">{value}</span>
      {hint && <span className="text-xs text-muted-foreground break-all">{hint}</span>}
    </div>
  );
}

function PoolStatusBadge({ status }: { status: string }) {
  if (status === "active") return <Badge variant="outline">active</Badge>;
  if (status === "paused") return <Badge variant="secondary">paused</Badge>;
  return <Badge variant="secondary">{status}</Badge>;
}

function PoolListCard({
  pools,
  selectedPoolId,
  onSelect
}: {
  pools: Pool[];
  selectedPoolId: string;
  onSelect: (id: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Earn pools</CardTitle>
        <CardDescription>{pools.length} on-chain · select a pool to inspect and act on.</CardDescription>
      </CardHeader>
      <CardContent>
        {pools.length === 0 ? (
          <Empty className="border">
            <EmptyHeader>
              <EmptyTitle>No pools registered</EmptyTitle>
              <EmptyDescription>
                Once an earn pool is registered on EarnManager, it will show here.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          <ScrollArea className="max-h-[20rem] pr-2">
            <ul className="flex flex-col gap-2">
              {pools.map((p) => {
                const selected = p.pool_id === selectedPoolId;
                return (
                  <li key={p.pool_id}>
                    <button
                      onClick={() => onSelect(p.pool_id)}
                      className={cn(
                        "w-full rounded-lg border px-3 py-2.5 text-left transition",
                        selected
                          ? "border-primary/40 bg-primary/5"
                          : "border-border hover:border-foreground/20 hover:bg-accent/50"
                      )}
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-xs">{shortHash(p.pool_id)}</span>
                        <PoolStatusBadge status={p.status} />
                      </div>
                      <div className="mt-1.5 flex items-center justify-between text-xs text-muted-foreground">
                        <Badge variant="secondary" className="font-mono">{p.strategy}</Badge>
                        <span className="font-mono">
                          AUM {fmtBaseUnits(p.total_assets, USDC_DECIMALS)} USDC
                        </span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          </ScrollArea>
        )}
      </CardContent>
    </Card>
  );
}

function PoolDetailCard({ pool, aave }: { pool: PoolDetail | null; aave: Aave | null }) {
  if (!pool) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Pool detail</CardTitle>
          <CardDescription>Select a pool from the list to inspect.</CardDescription>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    );
  }

  const onChainAssets = BigInt(pool.total_assets);
  const liveAssets = aave ? BigInt(aave.aTokenBalance) : null;
  const drift = liveAssets !== null ? liveAssets - onChainAssets : null;
  const driftClass =
    drift === null
      ? "text-muted-foreground"
      : drift > 0n
        ? "text-emerald-500"
        : drift < 0n
          ? "text-amber-500"
          : "text-muted-foreground";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Pool detail</CardTitle>
        <CardDescription className="font-mono break-all">{pool.pool_id}</CardDescription>
        <CardAction>
          <PoolStatusBadge status={pool.status} />
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Stat label="token_id" value={shortHash(pool.token_id)} hint={pool.token_id} />
        <Stat
          label="pool address (LP)"
          value={
            <a
              className="underline decoration-dotted underline-offset-2 hover:decoration-solid"
              target="_blank"
              rel="noreferrer"
              href={explorerAddr(pool.pool_address, "base-sepolia")}
            >
              {shortAddr(pool.pool_address)}
            </a>
          }
        />
        <Separator />
        <div className="grid grid-cols-2 gap-4">
          <Stat label="total shares" value={pool.total_shares} />
          <Stat
            label="effective AUM"
            value={`${fmtBaseUnits(pool.total_assets, USDC_DECIMALS)} USDC`}
            hint={liveAssets !== null ? `aToken: ${fmtBaseUnits(liveAssets.toString(), USDC_DECIMALS)} USDC` : undefined}
          />
        </div>
        {drift !== null && (
          <div className="flex flex-col gap-1">
            <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
              aToken vs on-chain totalAssets
            </span>
            <span className={cn("font-mono text-sm", driftClass)}>
              {drift === 0n
                ? "in sync"
                : `${drift > 0n ? "+" : ""}${fmtBaseUnits(drift.toString(), USDC_DECIMALS)} USDC (will sync on next deposit/withdraw)`}
            </span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function AaveCard({ aave, hasPool }: { aave: Aave | null; hasPool: boolean }) {
  if (!aave) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Aave V3 · Base Sepolia</CardTitle>
          <CardDescription>Live supply rate, aToken balance, and pool allowance.</CardDescription>
        </CardHeader>
        <CardContent>
          {hasPool ? <Skeleton className="h-32 w-full" /> : <p className="text-sm text-muted-foreground">No pool selected.</p>}
        </CardContent>
      </Card>
    );
  }
  const apyBps = Number(aave.supplyApyBps);
  const apy = apyBps / 100;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Aave V3 · Base Sepolia</CardTitle>
        <CardDescription>Supply rate {apy.toFixed(2)}% APY · {aave.supplyApyBps} bps</CardDescription>
        <CardAction>
          <Button
            variant="ghost"
            size="sm"
            nativeButton={false}
            render={
              <a href={explorerAddr(aave.pool, "base-sepolia")} target="_blank" rel="noreferrer" />
            }
          >
            Pool
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-4">
        <Stat
          label={`${aave.symbol} aToken (LP)`}
          value={fmtBaseUnits(aave.aTokenBalance, aave.decimals)}
          hint="principal + accrued yield"
        />
        <Stat
          label={`${aave.symbol} on LP wallet`}
          value={fmtBaseUnits(aave.underlyingBalance, aave.decimals)}
        />
        <Stat
          label="LP → pool allowance"
          value={fmtBaseUnits(aave.allowance, aave.decimals)}
          hint="topped up on demand by the strategy"
        />
        <Stat
          label="aToken contract"
          value={
            <a
              className="underline decoration-dotted underline-offset-2 hover:decoration-solid"
              target="_blank"
              rel="noreferrer"
              href={explorerAddr(aave.aTokenAddress, "base-sepolia")}
            >
              {shortAddr(aave.aTokenAddress)}
            </a>
          }
        />
      </CardContent>
    </Card>
  );
}

function ActionCard({
  poolId,
  amountHuman,
  onAmountChange,
  amountBaseUnits,
  quote,
  onPreview,
  onDeposit,
  onWithdraw,
  pendingAction,
  position,
  hasUserKey
}: {
  poolId: string;
  amountHuman: string;
  onAmountChange: (v: string) => void;
  amountBaseUnits: string;
  quote: Quote | null;
  onPreview: () => void;
  onDeposit: () => void;
  onWithdraw: () => void;
  pendingAction: null | "deposit" | "withdraw" | "quote";
  position: Position | undefined;
  hasUserKey: boolean;
}) {
  const disabled = !poolId || !amountBaseUnits || pendingAction !== null;
  const noKey = !hasUserKey;
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Actions</CardTitle>
          {noKey && <Badge variant="destructive">user key missing</Badge>}
        </div>
        <CardDescription>Deposit signs EIP-712 server-side. Withdraw uses the LP signer.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="amount">Amount</FieldLabel>
            <InputGroup>
              <InputGroupInput
                id="amount"
                inputMode="decimal"
                value={amountHuman}
                onChange={(e) => onAmountChange(e.target.value)}
                placeholder="1.0"
              />
              <InputGroupAddon align="inline-end">
                <InputGroupText>USDC</InputGroupText>
              </InputGroupAddon>
            </InputGroup>
            <FieldDescription>
              {amountBaseUnits ? `${amountBaseUnits} base units (6 decimals)` : "Enter a positive number"}
            </FieldDescription>
          </Field>

          <Field>
            <FieldLabel htmlFor="pool">Pool</FieldLabel>
            <InputGroup>
              <InputGroupInput id="pool" value={poolId} readOnly className="font-mono" />
            </InputGroup>
            <FieldDescription>Driven by the pool list above.</FieldDescription>
          </Field>
        </FieldGroup>

        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={onPreview} disabled={disabled || noKey}>
            {pendingAction === "quote" ? <Spinner data-icon="inline-start" /> : null}
            Preview quote
          </Button>
          <Button onClick={onDeposit} disabled={disabled || noKey}>
            {pendingAction === "deposit" ? <Spinner data-icon="inline-start" /> : null}
            Deposit
          </Button>
          <Button variant="outline" onClick={onWithdraw} disabled={disabled}>
            {pendingAction === "withdraw" ? <Spinner data-icon="inline-start" /> : null}
            Withdraw
          </Button>
        </div>

        {quote && (
          <div className="rounded-lg border bg-muted/40 p-4">
            <div className="mb-3 text-[11px] uppercase tracking-wider text-muted-foreground">Latest quote</div>
            <div className="grid grid-cols-2 gap-3 text-sm">
              <Stat label="shares estimate" value={quote.shares_estimate} />
              <Stat label="exchange rate" value={quote.exchange_rate} />
              <Stat label="transfer nonce" value={String(quote.transfer_nonce)} />
              <Stat
                label="pool address"
                value={
                  <a
                    className="underline decoration-dotted underline-offset-2 hover:decoration-solid"
                    target="_blank"
                    rel="noreferrer"
                    href={explorerAddr(quote.pool_address, "base-sepolia")}
                  >
                    {shortAddr(quote.pool_address)}
                  </a>
                }
              />
            </div>
          </div>
        )}
      </CardContent>
      <CardFooter className="flex flex-col items-stretch gap-2 border-t bg-muted/20 pt-4">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">Position in this pool</span>
        {position ? (
          <div className="grid grid-cols-3 gap-3 text-sm">
            <Stat label="shares" value={position.shares} />
            <Stat label="underlying" value={`${fmtBaseUnits(position.underlying_amount, USDC_DECIMALS)} USDC`} />
            <Stat label="rate" value={position.exchange_rate} />
          </div>
        ) : (
          <span className="text-sm text-muted-foreground">No shares yet.</span>
        )}
      </CardFooter>
    </Card>
  );
}

function ActivityCard({ entries }: { entries: ActivityEntry[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity</CardTitle>
        <CardDescription>Latest deposits & withdraws this session, with explorer links.</CardDescription>
      </CardHeader>
      <CardContent>
        {entries.length === 0 ? (
          <Empty className="border">
            <EmptyHeader>
              <EmptyTitle>No actions yet</EmptyTitle>
              <EmptyDescription>Deposit or withdraw to see results here.</EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <span className="text-xs text-muted-foreground">Each action records its tx hash with explorer link.</span>
            </EmptyContent>
          </Empty>
        ) : (
          <ScrollArea className="max-h-[26rem] pr-2">
            <ul className="flex flex-col gap-2">
              {entries.map((e, idx) => (
                <li key={`${e.ts}-${idx}`} className="rounded-lg border bg-muted/30 p-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <ActivityBadge kind={e.kind} />
                      <span className="text-sm">{e.label}</span>
                    </div>
                    <span className="text-xs text-muted-foreground">{new Date(e.ts).toLocaleTimeString()}</span>
                  </div>
                  {e.detail && (
                    <div className="mt-1 font-mono text-xs text-muted-foreground break-all">{e.detail}</div>
                  )}
                  {e.txHash && e.chain && (
                    <a
                      className="mt-2 inline-flex items-center gap-1 text-xs underline decoration-dotted underline-offset-2 hover:decoration-solid"
                      href={explorerTx(e.txHash, e.chain)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {shortHash(e.txHash)}
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </ScrollArea>
        )}
      </CardContent>
    </Card>
  );
}

function ActivityBadge({ kind }: { kind: ActivityKind }) {
  if (kind === "deposit") return <Badge variant="outline">deposit</Badge>;
  if (kind === "withdraw") return <Badge variant="secondary">withdraw</Badge>;
  return <Badge variant="destructive">error</Badge>;
}

function PositionsCard({ positions }: { positions: Position[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>All positions</CardTitle>
        <CardDescription>Live read of each pool's user shares + underlying value.</CardDescription>
      </CardHeader>
      <CardContent>
        {positions.length === 0 ? (
          <Empty className="border">
            <EmptyHeader>
              <EmptyTitle>No positions held</EmptyTitle>
              <EmptyDescription>Deposit USDC into a pool to mint shares.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          <ScrollArea className="w-full">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>pool_id</TableHead>
                  <TableHead>token_id</TableHead>
                  <TableHead className="text-right">shares</TableHead>
                  <TableHead className="text-right">underlying</TableHead>
                  <TableHead className="text-right">rate</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {positions.map((p) => (
                  <TableRow key={p.pool_id}>
                    <TableCell className="font-mono">{shortHash(p.pool_id)}</TableCell>
                    <TableCell className="font-mono">{shortHash(p.token_id)}</TableCell>
                    <TableCell className="text-right font-mono">{p.shares}</TableCell>
                    <TableCell className="text-right font-mono">
                      {fmtBaseUnits(p.underlying_amount, USDC_DECIMALS)} USDC
                    </TableCell>
                    <TableCell className="text-right font-mono">{p.exchange_rate}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollArea>
        )}
      </CardContent>
    </Card>
  );
}

function WalletConnect() {
  const { address, isConnected, status } = useAccount();
  const { connect, connectors, isPending: connecting } = useConnect();
  const { disconnect, isPending: disconnecting } = useDisconnect();
  const injectedConnector = connectors.find((c) => c.id === "injected") ?? connectors[0];

  if (isConnected && address) {
    return (
      <Button
        variant="secondary"
        size="sm"
        onClick={() => disconnect()}
        disabled={disconnecting}
      >
        {disconnecting ? <Spinner data-icon="inline-start" /> : null}
        {shortAddr(address)} · disconnect
      </Button>
    );
  }

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => injectedConnector && connect({ connector: injectedConnector })}
      disabled={connecting || !injectedConnector}
    >
      {connecting ? <Spinner data-icon="inline-start" /> : null}
      {status === "reconnecting" ? "Reconnecting…" : "Connect wallet"}
    </Button>
  );
}

function FlexvaultsLauncher() {
  return (
    <FlexvaultsButton
      variant="default"
      size="sm"
      hideWhenDisconnected={false}
      onDepositSuccess={() => toast.success("Deposit success", { description: "Funds en route" })}
    >
      Flexvaults wallet
    </FlexvaultsButton>
  );
}
