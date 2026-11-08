import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { noteRouteChange } from '../lib/routeChange'

/** Report each router pathname change to the listeners outside React (the
 *  socket's focus relay re-announces a Crew Mode chat entering or leaving the
 *  chat routes). */
export function useFocusRouteRelay(): void {
  const { pathname } = useLocation()

  useEffect(() => {
    noteRouteChange()
  }, [pathname])
}
