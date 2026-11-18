import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

/**
 * The configured default reasoning effort for new sessions (Settings > Chat),
 * or '' when none is set and the model picks its own. A session's own level
 * always outranks it, matching the backend's `reasoning_effort_override or
 * default`.
 *
 * Read straight from the shared `['kirocrewConfig']` entry rather than through
 * a query of its own: Settings invalidates that entry when it saves and the
 * gateway's config `refresh` frame does too, so a changed default shows in the
 * composer at once instead of after a reload. A failed read answers ''.
 */
export function useDefaultEffort(enabled: boolean): string {
  const { data } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled,
    select: (config: unknown) => (config as { agent?: { reasoning_effort?: string } } | undefined)?.agent?.reasoning_effort || '',
  })
  return data ?? ''
}
