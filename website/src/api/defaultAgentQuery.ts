import { api } from './client'

/**
 * The ONE definition of the shared ['default-agent'] query (issue #6495).
 *
 * Every consumer must spread this object rather than restating the key —
 * two inline spellings with different options would diverge silently.
 * useWebSocket invalidates this key on server refresh events; the finite
 * staleTime additionally opts into refetch-on-focus (the sanctioned pattern
 * per queryClient.ts), so a long-lived window self-heals after the default
 * changes in another window.
 *
 * The value is the default member's roster `name` (its display name): that is
 * what every consumer compares against `/api/agents` rows and renders in
 * labels. `default_agent` is the config KEY (the member_id) and is only the
 * fallback for a gateway that predates the `name` field.
 */
export const defaultAgentQuery = {
  queryKey: ['default-agent'] as const,
  queryFn: () =>
    api
      .defaultAgent()
      .then((d: { default_agent?: string; name?: string }) => d.name || d.default_agent || ''),
  staleTime: 30_000,
}
