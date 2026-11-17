import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { withDeadline } from '../lib/withDeadline'

/** Deadline on each look at `GET /api/terminal/sessions` during hydrate. The
 *  restored terminal tabs stay gated until both looks have settled, so a probe
 *  that never answers must be made to answer: past this bound it reads as a
 *  failed probe, which keeps every tab. Well above the route's normal
 *  round-trip (it reads an in-memory registry) and far below any wait a user
 *  would sit through for an empty panel. */
export const TERMINAL_PROBE_TIMEOUT_MS = 10_000

export type TerminalEnabledAnswer = { enabled?: boolean; locked_by_policy?: boolean; sessions?: unknown[] }

/**
 * The `terminal-enabled` cache without a fetch: a surface that mounts right
 * after a cache write (the Settings row, the off strip) reads what was written
 * and never issues the mount refetch that could land inside the gateway's gate
 * cache before the config watcher has applied a flip.
 */
export function useTerminalEnabledCache() {
  return useQuery<TerminalEnabledAnswer>({ queryKey: ['terminal-enabled'], enabled: false })
}

/**
 * `GET /api/terminal/sessions` as the `terminal-enabled` query. Besides the
 * refetches React Query schedules for it (mount, focus or reconnect after
 * `staleTime`), its cache is written directly by whoever learns the panel's
 * state first-hand: the registry's look after a refused socket and the Settings
 * switch's PATCH. Neither issues a refetch, so a refetch can never land inside
 * the gateway's 30 s gate cache before the config watcher has applied a flip.
 */
export function useTerminalEnabledQuery() {
  const qc = useQueryClient()
  return useQuery<TerminalEnabledAnswer>({
    queryKey: ['terminal-enabled'],
    // Bounded because the restored terminal tabs stay gated until this query
    // settles one way or the other: a request that hangs would otherwise hold
    // a blank panel open for as long as the socket did. Same bound as the
    // confirm look in the probe.
    queryFn: async ({ signal }) => {
      const r = await withDeadline(TERMINAL_PROBE_TIMEOUT_MS, signal, s => api.terminalSessions(s))
      // Default-on: the terminal is enabled unless the server explicitly says
      // otherwise. A transient/auth-timing failure of this probe must NOT hide
      // an enabled terminal by falling back to {enabled:false}, which with
      // staleTime would keep the panel hidden for 60s. Once an explicit false
      // has been seen, a failed refetch cannot rule either way and keeps it.
      if (!r.ok) {
        const prev = qc.getQueryData<TerminalEnabledAnswer>(['terminal-enabled'])
        return prev?.enabled === false ? prev : { enabled: true }
      }
      return r.json()
    },
    staleTime: 60_000,
  })
}
