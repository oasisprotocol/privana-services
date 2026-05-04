import { createPublicClient, createWalletClient, defineChain, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { env } from "./env";

const baseSepolia = defineChain({
  id: 84532,
  name: "Base Sepolia",
  nativeCurrency: { name: "Sepolia Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: { default: { http: [env.baseSepoliaRpc] } }
});

const sapphireTestnet = defineChain({
  id: env.accountingChainId,
  name: "Sapphire Testnet",
  nativeCurrency: { name: "TEST", symbol: "TEST", decimals: 18 },
  rpcUrls: { default: { http: [env.sapphireRpc] } }
});

export const baseClient = () =>
  createPublicClient({ chain: baseSepolia, transport: http(env.baseSepoliaRpc) });

export const sapphireClient = () =>
  createPublicClient({ chain: sapphireTestnet, transport: http(env.sapphireRpc) });

export const baseWalletClient = () => {
  if (!env.testUserPrivateKey || env.testUserPrivateKey.length <= 2) {
    throw new Error("TEST_USER_PRIVATE_KEY is not configured");
  }
  const account = privateKeyToAccount(env.testUserPrivateKey);
  return createWalletClient({
    account,
    chain: baseSepolia,
    transport: http(env.baseSepoliaRpc)
  });
};
