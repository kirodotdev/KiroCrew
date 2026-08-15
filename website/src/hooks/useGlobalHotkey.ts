import { useQuery } from '@tanstack/react-query'
import { isElectron } from '../lib/electron'
import type { GlobalHotkeyInfo } from '../lib/globalHotkey'

type HotkeyBridge = { getGlobalHotkey?: () => Promise<GlobalHotkeyInfo> }

/**
 * The desktop shell's system-wide summon hotkey, as ACTUALLY bound by the main
 * process (registration can degrade to the platform default or to nothing when
 * a key is taken by another application — see electron/global-hotkey.js).
 *
 * Returns null in a plain browser, on a desktop build without the bridge, and
 * when nothing is bound — callers hide the shortcut row entirely rather than
 * advertise a chord that does not work. The binding is fixed for the process
 * lifetime (registered once on app ready), hence `staleTime: Infinity`.
 */
export function useGlobalHotkey(): GlobalHotkeyInfo | null {
  const api = (window as Window & { electronAPI?: HotkeyBridge }).electronAPI
  const available = isElectron && typeof api?.getGlobalHotkey === 'function'
  const { data } = useQuery({
    queryKey: ['global-hotkey'],
    // `available` gates the query, so the bridge is present when this runs.
    queryFn: () => api!.getGlobalHotkey!(),
    enabled: available,
    staleTime: Infinity,
  })
  if (!available || !data || !data.accelerator) return null
  return data
}
