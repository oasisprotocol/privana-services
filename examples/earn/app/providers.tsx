"use client";

import { useState } from "react";
import "@oasisprotocol/flexvaults-sdk/styles.css";
import { FlexvaultsProvider } from "@oasisprotocol/flexvaults-sdk";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { WagmiProvider, createConfig, http } from "wagmi";
import { baseSepolia } from "wagmi/chains";
import { injected } from "wagmi/connectors";

const TOKEN_ID = process.env.NEXT_PUBLIC_DEFAULT_USDC_TOKEN_ID;
const enabledTokens = TOKEN_ID ? [TOKEN_ID] : [];

const wagmiConfig = createConfig({
  chains: [baseSepolia],
  connectors: [injected()],
  transports: { [baseSepolia.id]: http() }
});

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient());
  return (
    <WagmiProvider config={wagmiConfig}>
      <QueryClientProvider client={queryClient}>
        <FlexvaultsProvider tokens={enabledTokens}>{children}</FlexvaultsProvider>
      </QueryClientProvider>
    </WagmiProvider>
  );
}
