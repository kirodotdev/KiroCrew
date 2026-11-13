import { useCallback, useSyncExternalStore } from 'react'
import {
  subscribe,
  getSnapshot,
  openPopout,
  focusPopout,
  returnSelfToMain,
} from '../utils/appPopout'

/**
 * View of popped-out app pages for any surface. Backed by the shared
 * `appPopout` singleton, so every control that calls this hook subscribes to
 * one BroadcastChannel + heartbeat rather than spawning its own.
 *
 * Returns whether an app route is popped out plus the actions to open or
 * focus its window, and, from inside a pop-out, to return the app to main. A
 * pop-out never holds its OWN route in the map, so `isPoppedOut(ownRoute)`
 * reads false there; which window a surface is in is decided by where it is
 * rendered (the rail's row action in main, `AppPopoutTitleBar` in the
 * pop-out), not by this hook.
 */
export function useAppPopouts() {
  const poppedOut = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const isPoppedOut = useCallback((route: string) => poppedOut.has(route), [poppedOut])
  return {
    /** Whether an app route is currently open in a pop-out window (reactive). */
    isPoppedOut,
    /** Open an app in its own window at an app-space path+query (focuses the existing one if already out). */
    open: openPopout,
    /** Focus the existing pop-out window for an app. */
    focus: focusPopout,
    /** From inside a pop-out: return this window's app to the main dashboard. */
    returnSelfToMain,
  }
}
