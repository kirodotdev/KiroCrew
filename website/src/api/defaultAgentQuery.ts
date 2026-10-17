import { api } from './client'
import { refetchWhileFailed, retryThroughRestart } from './queryClient'

/** What `GET /api/config/default-agent` answers. */
export type DefaultAgentReadout = { default_agent: string; default_template: string; overridden: boolean; override_path?: string }

/**
 * The ONE definition of the shared ['default-agent'] query (issue #6495).
 *
 * The ONE cache for `GET /api/config/default-agent`: it carries the whole
 * readout. `default_template` is the default custom agent (a template), the
 * agent an agent-less slot or cron RUNS; consumers that only want that name
 * `select` it. Labels of such a row (`agentOrDefaultLabel`) must read the
 * template, not the crewmate alias `default_agent` -- a default crewmate
 * `radar` with `agent.default_agent` `kirocrew` would label a cron that runs
 * `kirocrew` as radar's. A gateway without the fields yields '' / false, and
 * the label degrades to the literal `default`. The Custom agents tab reads
 * the full object (`overridden`, `override_path`) through this same key.
 *
 * Every consumer must spread this object rather than restating the key —
 * two inline spellings with different options would diverge silently.
 * useWebSocket invalidates this key on server refresh events; the finite
 * staleTime additionally opts into refetch-on-focus (the sanctioned pattern
 * per queryClient.ts), so a long-lived window self-heals after the default
 * changes in another window.
 */
export const defaultAgentQuery = {
  queryKey: ['default-agent'] as const,
  queryFn: (): Promise<DefaultAgentReadout> => api.defaultAgent().then((d: Partial<DefaultAgentReadout>) => ({
    ...d,
    default_agent: d.default_agent ?? '',
    default_template: d.default_template ?? '',
    overridden: d.overridden ?? false,
  })),
  staleTime: 30_000,
  // A failed read is a standing notice on the Crewmates page, so a gateway
  // restart is ridden out quietly first (see `retryThroughRestart`).
  retry: retryThroughRestart,
  // ...and a failed read keeps re-reading, so its notice clears on its own.
  refetchInterval: refetchWhileFailed,
}
