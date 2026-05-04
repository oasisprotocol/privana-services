import type { Address, Hex } from "viem";

export const env = {
  apiBaseUrl: process.env.API_BASE_URL ?? "http://localhost:8001",
  testUserAddress: (process.env.TEST_USER_ADDRESS ?? "0x0000000000000000000000000000000000000000") as Address,
  testUserPrivateKey: (process.env.TEST_USER_PRIVATE_KEY ?? "") as Hex,
  lpAddress: (process.env.LP_ADDRESS ?? "0x0000000000000000000000000000000000000000") as Address,
  accountingContract: (process.env.ACCOUNTING_CONTRACT_ADDRESS ?? "0x0000000000000000000000000000000000000000") as Address,
  accountingChainId: Number(process.env.ACCOUNTING_CHAIN_ID ?? 23295),
  sapphireRpc: process.env.SAPPHIRE_RPC_URL ?? "https://testnet.sapphire.oasis.io",
  baseSepoliaRpc: process.env.BASE_SEPOLIA_RPC_URL ?? "https://sepolia.base.org",
  aavePool: (process.env.AAVE_POOL_ADDRESS ?? "0x0000000000000000000000000000000000000000") as Address,
  aaveUsdc: (process.env.AAVE_USDC_ADDRESS ?? "0x0000000000000000000000000000000000000000") as Address,
  defaultPoolId: (process.env.DEFAULT_POOL_ID ?? "") as Hex,
  defaultUsdcTokenId: (process.env.DEFAULT_USDC_TOKEN_ID ?? "") as Hex
} as const;

export type PublicEnv = {
  apiBaseUrl: string;
  testUserAddress: Address;
  lpAddress: Address;
  accountingContract: Address;
  accountingChainId: number;
  sapphireRpc: string;
  baseSepoliaRpc: string;
  aavePool: Address;
  aaveUsdc: Address;
  defaultPoolId: Hex;
  defaultUsdcTokenId: Hex;
  hasUserKey: boolean;
};

export const publicEnv = (): PublicEnv => ({
  apiBaseUrl: env.apiBaseUrl,
  testUserAddress: env.testUserAddress,
  lpAddress: env.lpAddress,
  accountingContract: env.accountingContract,
  accountingChainId: env.accountingChainId,
  sapphireRpc: env.sapphireRpc,
  baseSepoliaRpc: env.baseSepoliaRpc,
  aavePool: env.aavePool,
  aaveUsdc: env.aaveUsdc,
  defaultPoolId: env.defaultPoolId,
  defaultUsdcTokenId: env.defaultUsdcTokenId,
  hasUserKey: env.testUserPrivateKey.length > 2
});
