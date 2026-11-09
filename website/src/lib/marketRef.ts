/**
 * Parse what the person typed into the Install from Kiro Space field:
 * `<namespace>/<name>` with an optional `@<version>`.
 *
 * The rule is the gateway's own (`LISTING_REF_RE` in `apps/market_source.py`:
 * two lowercase kebab segments, and a positive integer of at most nine digits
 * for the version), so a reference this accepts is one the routes accept and a
 * bad one never leaves the page. The gateway stays the authority; this only
 * saves a round trip.
 */
const SEGMENT = '[a-z0-9]+(?:-[a-z0-9]+)*'
const MARKET_REF_RE = new RegExp(`^(${SEGMENT}/${SEGMENT})(?:@([1-9][0-9]{0,8}))?$`)

export function parseMarketRef(input: string): { ref: string; version?: number } | null {
  const m = MARKET_REF_RE.exec(input.trim())
  if (!m) return null
  return m[2] === undefined ? { ref: m[1] } : { ref: m[1], version: Number(m[2]) }
}
