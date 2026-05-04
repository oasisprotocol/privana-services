export const fmtBaseUnits = (value: string | bigint, decimals: number): string => {
  const v = typeof value === "string" ? BigInt(value) : value;
  if (decimals === 0) return v.toString();
  const negative = v < 0n;
  const abs = negative ? -v : v;
  const base = 10n ** BigInt(decimals);
  const whole = abs / base;
  const frac = abs % base;
  if (frac === 0n) return `${negative ? "-" : ""}${whole.toString()}`;
  const fracStr = frac.toString().padStart(decimals, "0").replace(/0+$/, "");
  return `${negative ? "-" : ""}${whole.toString()}.${fracStr}`;
};

export const shortAddr = (addr: string, head = 6, tail = 4): string =>
  addr.length <= head + tail + 2 ? addr : `${addr.slice(0, head)}…${addr.slice(-tail)}`;

export const shortHash = (hash: string): string => shortAddr(hash, 10, 8);
