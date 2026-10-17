import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { crewLogProjectionsKey } from '../../hooks/useWebSocket'

const rec = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}

/** A token count, or 0 for anything a count cannot be: absent, negative, NaN, a string. */
const count = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0)

/** The token block of the `usage` fold in a crew-log read, whatever shape came back. */
const tokensOf = (read: unknown): unknown => rec(rec(rec(rec(read).folds).usage).value).tokens

/** The turn counts of the `usage` fold in a crew-log read, whatever shape came back. */
const turnsOf = (read: unknown): unknown => rec(rec(rec(rec(read).folds).usage).value).turns

/** The share of a session's input tokens served from the prompt cache, from the
 *  crew log's `usage` fold: cache reads over cache reads, cache writes and
 *  uncached input (the fold's four token counts are disjoint).
 *
 *  `null` says "nothing to show", never zero. A backend that sends no token
 *  counts, or sends input tokens but no cache counts, leaves both cache counts at
 *  0, which would compute to a 0% miss. Only a non-zero cache count proves the
 *  backend reports cache at all, so without one there is no ratio. */
export function cacheHitRatio(read: unknown): number | null {
  const tokens = tokensOf(read)
  const hits = count(rec(tokens).cache_read)
  const writes = count(rec(tokens).cache_write)
  if (hits + writes === 0) return null
  const total = hits + writes + count(rec(tokens).input)
  return Number.isFinite(total) ? hits / total : null
}

/** Whether the session's backend reports token counts at all, read from the fold's
 *  own count of the turns that did (`turns.tokens_reported`). A turn counts only
 *  when one of its four counts is above zero, so a backend that sends none stays at
 *  0 (kiro-cli's prompt response carries only a stop reason). It tells a backend
 *  that reports tokens but no cache counts yet (the popover says so) from one that
 *  never reports any (the popover stays silent: nothing to wait for). */
export function reportsTokens(read: unknown): boolean {
  return count(rec(turnsOf(read)).tokens_reported) > 0
}

/** The ratio for the context popover, read from the entry the Crew Log tab fills,
 *  so a pushed `session_projection` frame keeps it current. The caller mounts it
 *  while the popover is open, which is when the read starts.
 *
 *  `failed` is a read that errored with nothing earlier to show: a null ratio from
 *  a backend that reports no cache counts is a successful read and not a failure.
 *  A later read that fails keeps the ratio the last good one produced. */
export function useCacheHitRatio(slot: string | null): { ratio: number | null; failed: boolean; reported: boolean } {
  const { data, isError } = useQuery({
    queryKey: crewLogProjectionsKey(slot ?? ''),
    queryFn: () => api.sessionCrewLogProjections(slot as string),
    enabled: !!slot,
    staleTime: 30_000,
  })
  return { ratio: cacheHitRatio(data), failed: isError && data === undefined, reported: reportsTokens(data) }
}
