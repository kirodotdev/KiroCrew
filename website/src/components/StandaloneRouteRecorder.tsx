import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { recordStandaloneRoute } from '../lib/standaloneLastRoute'

/**
 * Record each route the installed web app shows, so its next launch at `/`
 * reopens it (lib/standaloneLastRoute.ts). Mounted once inside the router and
 * outside the routes. Renders nothing, and does nothing in a browser tab.
 */
export function StandaloneRouteRecorder() {
  const { pathname, search } = useLocation()
  useEffect(() => {
    recordStandaloneRoute(`${pathname}${search}`)
  }, [pathname, search])
  return null
}
