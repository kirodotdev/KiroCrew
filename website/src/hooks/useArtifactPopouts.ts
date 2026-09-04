import { useCallback, useSyncExternalStore } from 'react'
import {
  subscribe,
  getSnapshot,
  openPopout,
  focusPopout,
  bringBack,
  isSelfPopout,
  returnSelfToMain,
} from '../utils/artifactPopout'

/**
 * View of popped-out artifacts for any surface. Backed by the shared
 * `artifactPopout` singleton, so every control that calls this hook subscribes
 * to one BroadcastChannel + heartbeat rather than spawning its own.
 *
 * Returns the reactive set of popped-out artifact slugs plus the actions to
 * open, focus, or bring one back. Surfaces that also render inside a popout
 * window must check `isSelfPopout` — a popout never holds its OWN slug in the
 * map, so `isPoppedOut(ownSlug)` reads false there.
 */
export function useArtifactPopouts() {
  const poppedOut = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const isPoppedOut = useCallback((slug: string) => poppedOut.has(slug), [poppedOut])
  return {
    /** Reactive set of artifact slugs currently open in a popout window. */
    poppedOut,
    isPoppedOut,
    /** True when THIS window is itself the popout for the slug (stable per window lifetime). */
    isSelfPopout,
    /** Open an artifact in its own window (focuses the existing one if already out). */
    open: openPopout,
    /** Focus the existing popout window for an artifact. */
    focus: focusPopout,
    /** Close an artifact's popout window (caller re-views it in the main window). */
    bringBack,
    /** From inside a popout: return this window's artifact to the main dashboard. */
    returnSelfToMain,
  }
}
