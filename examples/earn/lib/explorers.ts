export type Chain = "base-sepolia" | "sapphire-testnet";

export const explorerTx = (hash: string, chain: Chain): string =>
  chain === "base-sepolia"
    ? `https://sepolia.basescan.org/tx/${hash}`
    : `https://explorer.oasis.io/testnet/sapphire/tx/${hash}`;

export const explorerAddr = (addr: string, chain: Chain): string =>
  chain === "base-sepolia"
    ? `https://sepolia.basescan.org/address/${addr}`
    : `https://explorer.oasis.io/testnet/sapphire/address/${addr}`;
