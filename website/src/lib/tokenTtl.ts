/**
 * How long the token a crew is holding was issued for.
 *
 * Every reader that asks "how far through its life is this token" divides the
 * gateway's `token_ttl_remaining` by a total, and the two must be the SAME
 * number. They are not interchangeable: a chained crew's token is minted by the
 * crew that holds the hop, under that crew's own record, so the stored total is
 * the parent's figure while the row here carries an unrelated one that defaults
 * to 20h. Dividing a 1h token's remaining by a 20h row puts it permanently past
 * any refresh threshold, and the reader re-mints on every poll -- which remounts
 * the crew's iframe and discards whatever was unsaved in it.
 *
 * So the gateway publishes the total it counted down from, and this is the one
 * place that picks it. Three callers share it rather than each deriving a total,
 * because a fourth deriving its own is exactly the defect.
 */

/** A crew's TTL string, as the registry stores it: `<int>h` or `<int>m`. */
export function ttlToSeconds(ttl: string | undefined): number {
  const m = /^(\d+)([hm])$/.exec(ttl || '')
  if (!m) return 0
  const n = Number(m[1])
  return m[2] === 'h' ? n * 3600 : n * 60
}

/** The status fields this decision reads. Both are optional over the wire. */
export interface TokenTtlStatus {
  token_ttl_remaining?: number
  token_ttl_total?: number
}

/**
 * Seconds the current token was issued for.
 *
 * Prefers the gateway's own `token_ttl_total`, which is the number its
 * `token_ttl_remaining` counts down from. Falls back to the row's TTL, which is
 * the correct total for every crew this gateway mints for itself and is what an
 * older gateway that does not publish the field would have meant. A
 * non-positive or non-finite published value is not trusted, because a zero
 * total would make every comparison against it read as expired.
 */
export function tokenTtlTotalSeconds(
  status: TokenTtlStatus | undefined,
  rowTtl: string | undefined,
): number {
  const published = status?.token_ttl_total
  if (typeof published === 'number' && Number.isFinite(published) && published > 0) {
    return published
  }
  return ttlToSeconds(rowTtl)
}
