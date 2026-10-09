import { api } from './client'
import { refetchWhileFailed, retryThroughRestart } from './queryClient'

/**
 * The ONE definition of the shared ['default-agent'] query (issue #6495).
 *
 * Resolves to the default custom agent (a template): `default_template` on
 * `GET /api/config/default-agent`, the agent an agent-less slot or cron RUNS.
 * Its readers label such a row (`agentOrDefaultLabel`), so the crewmate alias
 * the same route also returns is the wrong answer here -- a default crewmate
 * `radar` with `agent.default_agent` `kirocrew` would label a cron that runs
 * `kirocrew` as radar's. A gateway without the field yields '', and the label
 * degrades to the literal `default`.
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
  queryFn: () => api.defaultAgent().then((d: { default_template?: string }) => d.default_template || ''),
  staleTime: 30_000,
  // A failed read is a standing notice on the Crewmates page, so a gateway
  // restart is ridden out quietly first (see `retryThroughRestart`).
  retry: retryThroughRestart,
  // ...and a failed read keeps re-reading, so its notice clears on its own.
  refetchInterval: refetchWhileFailed,
}
