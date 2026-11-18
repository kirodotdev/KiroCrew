import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { usePersistedBool } from '../hooks/usePersistedBool'
import { STANDALONE_REOPEN_KEY, recordStandaloneRoute } from '../lib/standaloneLastRoute'

/**
 * Record each route the installed web app shows, so its next launch at `/`
 * reopens it (lib/standaloneLastRoute.ts). Mounted once inside the router and
 * outside the routes. Renders nothing, and does nothing in a browser tab or
 * while "Reopen where I left off" is off. It follows that switch live, so the
 * page showing when it is turned on is recorded without another navigation.
 */
export function StandaloneRouteRecorder() {
  const { pathname, search } = useLocation()
  const [reopenEnabled] = usePersistedBool(STANDALONE_REOPEN_KEY, false)
  useEffect(() => {
    if (reopenEnabled) recordStandaloneRoute(`${pathname}${search}`)
  }, [pathname, search, reopenEnabled])
  return null
}
