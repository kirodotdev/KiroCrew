/**
 * The `connections_ui` flag — one predicate, every surface that needs it.
 *
 * The Connections gallery ships ON. The flag survives as an escape hatch: set
 * `connections_ui: false` in the running instance's `$KIROCREW_HOME/config.json`
 * and every Connections surface goes away again — the gallery offers no cards,
 * and chat is once more the only authorize prompt. Config is read live, so no
 * gateway restart is needed in either direction.
 *
 * WHICH providers a launched gallery offers is a separate, per-provider decision
 * this flag does not touch: pages/connections/registry.ts ships only registry
 * entries whose `launch_gate_passed` is set, so a provider can stay held back
 * while the gallery itself is on.
 *
 * Chat needs the same answer as the gallery. A card-owned OAuth request is worth
 * hiding from chat only when the card that owns it is actually on screen; with
 * the hatch pulled there is no card, so chat is the user's only authorize
 * prompt. Deriving both from one predicate and one `['kirocrewConfig']` cache
 * entry is what keeps them from disagreeing about whether Connections exists.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

const CONNECTIONS_UI_FLAG = 'connections_ui'

/**
 * ON unless this instance opted out.
 *
 * An absent flag is the shipped default and resolves ON. Only an exact `true`
 * re-asserts it, so a hand-edited `"false"` or `0` reads as OFF rather than
 * silently failing to disable the feature: the same "never guess from a sloppy
 * value" rule the opt-in version enforced, now pointed at the safe direction,
 * because the safe direction is off.
 *
 * A config that has not been read yet — `undefined`, and a failed fetch — stays
 * OFF. The opt-out lives in that config, so until it is read there is no basis
 * to honour it, and defaulting ON here would flash a gallery the user turned off
 * and fire its status queries on their behalf.
 */
export function connectionsUiEnabled(config: unknown): boolean {
  if (config === null || typeof config !== 'object') return false
  const value = (config as Record<string, unknown>)[CONNECTIONS_UI_FLAG]
  return value === undefined || value === true
}

/** Live flag value, off the shared `['kirocrewConfig']` query cache. */
export function useConnectionsUiEnabled(): boolean {
  const { data } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  return connectionsUiEnabled(data)
}
