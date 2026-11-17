import { useCallback, useContext } from 'react'
import { UNSAFE_NavigationContext, useLocation } from 'react-router-dom'

/**
 * Returns a check that the location this component rendered with is still the
 * history's current entry.
 *
 * `BrowserRouter` applies each history change inside `startTransition`, so the
 * `useLocation()` value a render sees can lag the address bar: a click
 * navigates, and an unrelated urgent update (a ResizeObserver measurement)
 * renders and commits first, still carrying the previous location. An effect
 * from that render that writes a replace navigation built from the rendered
 * location would overwrite the click's entry with the page the user just left.
 * Gate such self-correcting writes on this check. Skipping is safe: the check
 * changes identity with the location key, so an effect that lists it as a
 * dependency runs again once the router renders the newer entry.
 *
 * A navigator without a readable `location` (a hand-built one) reports
 * current, which keeps the write.
 */
export function useRenderedLocationIsCurrent(): () => boolean {
  const { navigator } = useContext(UNSAFE_NavigationContext)
  const { key } = useLocation()
  return useCallback(() => {
    const live = (navigator as { location?: { key?: unknown } }).location
    return typeof live?.key !== 'string' || live.key === key
  }, [navigator, key])
}
