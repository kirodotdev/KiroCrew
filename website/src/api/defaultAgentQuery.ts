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
 */
export const defaultAgentQuery = {
  queryKey: ['default-agent'] as const,
  queryFn: () => api.defaultAgent().then((d: { default_agent?: string }) => d.default_agent || ''),
  staleTime: 30_000,
}
