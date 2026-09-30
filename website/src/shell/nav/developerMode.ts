import { useEffect, useState } from 'react'
import { setNativeDevMode } from '../platform/electronBridge'

/**
 * Developer mode as the rail's Developer row reads it: the mode itself (flipped
 * from Settings > Developer), the unvisited-page dot, and the Electron View >
 * DevTools menu kept in step with it.
 */
export function useDeveloperMode(pathname: string) {
  const [devMode, setDevMode] = useState(() => localStorage.getItem('mc-dev-mode') === '1')
  const [devPageSeen, setDevPageSeen] = useState(true)
  // Listen for dev mode changes from Settings > Developer
  useEffect(() => {
    const handler = (e: Event) => {
      const enabled = (e as CustomEvent).detail
      setDevMode(enabled)
      if (enabled) setDevPageSeen(false)
    }
    window.addEventListener('mc-dev-mode-changed', handler)
    return () => window.removeEventListener('mc-dev-mode-changed', handler)
  }, [])
  // Sync dev-mode state to Electron on startup (so View > DevTools menu is correct)
  useEffect(() => {
    setNativeDevMode(devMode)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // Dismiss the dev-page notification dot once the user visits /developer
  useEffect(() => {
    if (pathname === '/developer') setDevPageSeen(true)
  }, [pathname])
  return { devMode, devPageSeen }
}
