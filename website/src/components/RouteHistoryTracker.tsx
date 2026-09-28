import { useEffect } from 'react'
import { useLocation, useNavigationType } from 'react-router-dom'
import { recordRouteNavigation } from '../lib/routeHistoryPosition'

/**
 * Feed the route-history position store (#8258) — mounted ONCE inside the
 * router, next to `NavigationBackGuard` in main.tsx. The ⌘/Ctrl+←/→ chords and
 * the desktop View > Back/Forward menu items read that store to know whether a
 * step has anywhere to go. Renders nothing.
 */
export function RouteHistoryTracker() {
  const location = useLocation()
  const navigationType = useNavigationType()
  // After commit, keyed on the entry: `location.key` changes on every
  // navigation including same-path pushes, and reading `history.state.idx`
  // after commit is what `NavigationBackGuard`'s own tracking effect does.
  useEffect(() => {
    recordRouteNavigation(navigationType)
  }, [location.key, navigationType])
  return null
}
