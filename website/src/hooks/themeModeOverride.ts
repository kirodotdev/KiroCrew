/**
 * A dark or light mode painted over the person's own choice for a while, never
 * saved. Set while a relay Crew is in front (RelayCrewPaneHost): that Crew's page
 * fills the window under the local top bar, so the bar follows the page's mode,
 * as an SSH Crew's header does by being the pane's own. Cleared, the person's
 * mode comes back as it was.
 */
import { useSyncExternalStore } from 'react'

export type ThemeModeOverride = 'dark' | 'light' | null

let current: ThemeModeOverride = null
const listeners = new Set<() => void>()

export function setThemeModeOverride(mode: ThemeModeOverride): void {
  if (mode === current) return
  current = mode
  for (const cb of listeners) cb()
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

export function useThemeModeOverride(): ThemeModeOverride {
  return useSyncExternalStore(subscribe, () => current, () => null)
}
