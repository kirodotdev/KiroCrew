/**
 * The `market:` source marker an app installed from Kiro Space carries.
 *
 * Mirrors `src/kiro_crew/apps/market_source.py` exactly: `market:<ns>/<name>@<n>`,
 * both ref segments lowercase kebab, the version a positive integer of at most
 * nine digits with no leading zero. Anything else is not a market source.
 */
const MARKET_SOURCE_RE = /^market:([a-z0-9]+(?:-[a-z0-9]+)*\/[a-z0-9]+(?:-[a-z0-9]+)*)@([1-9][0-9]{0,8})$/

export interface MarketSource {
  ref: string
  version: number
}

/** Parse a well-formed market source, or null for anything else. */
export function parseMarketSource(source: unknown): MarketSource | null {
  if (typeof source !== 'string') return null
  const match = MARKET_SOURCE_RE.exec(source)
  return match ? { ref: match[1], version: Number(match[2]) } : null
}
