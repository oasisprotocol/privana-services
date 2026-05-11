import { privateKeyToAccount } from "viem/accounts";
import type { Address, Hex } from "viem";

import { env } from "./env";

const TRANSFER_TYPES = {
  Transfer: [
    { name: "toAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" }
  ]
} as const;

const WITHDRAW_TYPES = {
  Withdraw: [
    { name: "poolId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" }
  ]
} as const;

export const signTransfer = async (params: {
  toAddress: Address;
  tokenId: Hex;
  amount: bigint;
  nonce: number;
}): Promise<Hex> => {
  if (!env.testUserPrivateKey || env.testUserPrivateKey.length <= 2) {
    throw new Error("TEST_USER_PRIVATE_KEY is not configured");
  }
  const account = privateKeyToAccount(env.testUserPrivateKey);
  return account.signTypedData({
    domain: {
      name: "AccountingModule",
      version: "1",
      chainId: env.accountingChainId,
      verifyingContract: env.accountingContract
    },
    types: TRANSFER_TYPES,
    primaryType: "Transfer",
    message: {
      toAddress: params.toAddress,
      tokenId: params.tokenId,
      amount: params.amount,
      nonce: BigInt(params.nonce)
    }
  });
};

export const signWithdrawConsent = async (params: {
  poolId: Hex;
  amount: bigint;
  nonce: number;
}): Promise<Hex> => {
  if (!env.testUserPrivateKey || env.testUserPrivateKey.length <= 2) {
    throw new Error("TEST_USER_PRIVATE_KEY is not configured");
  }
  if (!env.earnManagerContract || env.earnManagerContract.length <= 2) {
    throw new Error("EARN_MANAGER_CONTRACT_ADDRESS is not configured");
  }
  const account = privateKeyToAccount(env.testUserPrivateKey);
  return account.signTypedData({
    domain: {
      name: "EarnManager",
      version: "1",
      chainId: env.accountingChainId,
      verifyingContract: env.earnManagerContract
    },
    types: WITHDRAW_TYPES,
    primaryType: "Withdraw",
    message: {
      poolId: params.poolId,
      amount: params.amount,
      nonce: BigInt(params.nonce)
    }
  });
};
