/**
 * The `connections_ui` opt-in flag — one predicate, every surface that needs it.
 *
 * The Connections gallery is merged on main but held for a later release, so it
 * is reachable only when `connections_ui: true` is set in the running instance's
 * `$KIROCREW_HOME/config.json`. Config is read live, so no gateway restart is
 * needed.
 *
 * Chat needs the same answer as the gallery. A card-owned OAuth request is worth
 * hiding from chat only when the card that owns it is actually on screen; behind
 * a closed flag chat is still the user's only authorize prompt. Deriving both
 * from one predicate and one `['kirocrewConfig']` cache entry is what keeps them
 * from disagreeing about whether Connections exists.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

const CONNECTIONS_UI_FLAG = 'connections_ui'

/** Absent config, a failed fetch, and truthy-but-not-`true` all resolve to false. */
export function connectionsUiEnabled(config: unknown): boolean {
  return (config as Record<string, unknown> | undefined)?.[CONNECTIONS_UI_FLAG] === true
}

/** Live flag value, off the shared `['kirocrewConfig']` query cache. */
export function useConnectionsUiEnabled(): boolean {
  const { data } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  return connectionsUiEnabled(data)
}
