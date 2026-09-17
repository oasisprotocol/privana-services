const PRIVANA_API_URLS: Record<string, string> = {
  'sapphire': 'https://api.privana.finance',
  'sapphire-testnet': 'https://api.testnet.privana.finance',
};

// Resolves a tokenId to "SYMBOL (Chain)" via the Privana accounting API, so the operator can
// sanity-check the raw hash before confirming. Returns 'N/A' on networks that have no
// corresponding API (e.g. sapphire-localnet). Throws if the API does not know the tokenId.
export async function describeToken(networkName: string, tokenId: string): Promise<string> {
  const apiUrl = PRIVANA_API_URLS[networkName];
  if (!apiUrl) return 'N/A';

  let tokens: any[];
  try {
    const res = await fetch(`${apiUrl}/v1/accounting/tokens`);
    if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`);
    ({ tokens } = await res.json());
  } catch (e) {
    return `token lookup failed: ${e}`;
  }

  const token = tokens.find((t: any) => t.token_id.toLowerCase() === tokenId.toLowerCase());
  if (!token) {
    throw new Error(`Token ${tokenId} is unknown to the Privana accounting API at ${apiUrl}`);
  }
  return `${token.symbol} (${token.chain_name})`;
}
