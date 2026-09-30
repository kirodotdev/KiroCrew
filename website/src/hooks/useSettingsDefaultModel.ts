import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

/**
 * Settings → Chat → Default Model, read from the shared `['kirocrewConfig']`
 * entry, which every Settings write updates. `''` when none is set (unset or
 * `auto`). `null` while the body is unknown -- still loading, a read that
 * failed with no body on hand (the page's config-read notice reports that), or
 * a remote session, whose default lives on the peer -- so no caller claims a
 * default it cannot see.
 */
export function useSettingsDefaultModel(remote = false): string | null {
  const { data } = useQuery({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: !remote,
  })
  if (remote || !data) return null
  const model = (data as { agent?: { model?: unknown } }).agent?.model
  return typeof model === 'string' && model !== 'auto' ? model : ''
}
