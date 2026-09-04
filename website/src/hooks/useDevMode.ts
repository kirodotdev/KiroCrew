import { useEffect, useState } from 'react'

const DEV_MODE_KEY = 'mc-dev-mode'
const DEV_MODE_EVENT = 'mc-dev-mode-changed'

/**
 * Reactive read of the Developer Mode consent gate (Settings > Developer).
 *
 * Mirrors the flag App.tsx uses to gate the standalone Developer page, so a
 * surface gated on this hook appears/disappears in lockstep with the sidebar
 * entry. Updates live on the `mc-dev-mode-changed` event the toggle fires, and
 * on cross-tab `storage` events so a second tab stays consistent.
 */
export function useDevMode(): boolean {
  const [devMode, setDevMode] = useState(() => localStorage.getItem(DEV_MODE_KEY) === '1')
  useEffect(() => {
    const onEvent = (e: Event) => setDevMode(!!(e as CustomEvent<boolean>).detail)
    const onStorage = (e: StorageEvent) => {
      if (e.key === DEV_MODE_KEY) setDevMode(e.newValue === '1')
    }
    window.addEventListener(DEV_MODE_EVENT, onEvent)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(DEV_MODE_EVENT, onEvent)
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  return devMode
}
