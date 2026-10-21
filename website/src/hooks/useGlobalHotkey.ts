import { useQuery } from '@tanstack/react-query'
import { isElectron } from '../lib/electron'
import type { GlobalHotkeyInfo } from '../lib/globalHotkey'

/**
 * The desktop shell's system-wide summon hotkey, as reported by the main
 * process (registration can degrade to the platform default or to nothing when
 * a key is taken by another application — see electron/global-hotkey.js).
 *
 * Returns null in a plain browser and on a desktop build without the bridge:
 * there is no desktop shell to configure, so there is nothing to show. On a
 * desktop build it returns the info even when the hotkey is UNBOUND
 * (`accelerator: ""`, the fresh-install opt-out default), so the Settings UI
 * can show the summon shortcut as "Off" with how-to-enable guidance rather than
 * hide it — a hidden switch cannot be opted back into. Callers that must not
 * advertise a dead chord check `accelerator` themselves. The binding is fixed
 * for the process lifetime (registered once on app ready), hence
 * `staleTime: Infinity`.
 */
export function useGlobalHotkey(): GlobalHotkeyInfo | null {
  const api = window.electronAPI
  const available = isElectron && typeof api?.getGlobalHotkey === 'function'
  const { data } = useQuery({
    queryKey: ['global-hotkey'],
    // `available` gates the query, so the bridge is present when this runs.
    queryFn: () => api!.getGlobalHotkey!(),
    enabled: available,
    staleTime: Infinity,
  })
  if (!available || !data) return null
  return data
}
