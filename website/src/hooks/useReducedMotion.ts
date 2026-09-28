/**
 * The user's motion preference, read LIVE: `prefers-reduced-motion` is a system
 * setting that can flip while the page is open, and a paused timeline must
 * follow it.
 *
 * Not `framer-motion`'s `useReducedMotion`: that one snapshots the preference into
 * a `useState` at mount (`use-reduced-motion.mjs` — `const [shouldReduceMotion] =
 * useState(prefersReducedMotion.current)`, with a TODO beside it) and never
 * re-renders on change, so a preference flipped mid-session would not pause a
 * running animation. This hook subscribes to the media query's `change` event.
 *
 * Total against engines with no `matchMedia` (a test environment, an old
 * runtime): reads as "no preference" there.
 */
import { useEffect, useState } from 'react'

const QUERY = '(prefers-reduced-motion: reduce)'

function mediaQuery(): MediaQueryList | null {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(QUERY)
    : null
}

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => mediaQuery()?.matches ?? false)
  useEffect(() => {
    const mq = mediaQuery()
    if (!mq) return
    const onChange = () => setReduced(mq.matches)
    // The mount-time read above and the first commit can straddle a flip.
    onChange()
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])
  return reduced
}
