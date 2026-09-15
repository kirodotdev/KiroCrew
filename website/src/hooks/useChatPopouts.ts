import { useCallback, useSyncExternalStore } from 'react'
import {
  subscribe,
  getSnapshot,
  openPopout,
  focusPopout,
  bringBack,
  isSelfPopout,
  returnSelfToMain,
} from '../utils/chatPopout'

/**
 * View of popped-out chat sessions for any surface. Backed by the shared
 * `chatPopout` singleton, so every menu/row that calls this hook subscribes to
 * one BroadcastChannel + heartbeat rather than spawning its own.
 *
 * Returns the reactive set of popped-out slot keys plus the actions to open,
 * focus, or bring a session back. Surfaces that also render inside a popout
 * window (the session menu does) must check `isSelfPopout` — a popout never
 * holds its OWN slot in the map, so `isPoppedOut(ownSlot)` reads false there.
 */
export function useChatPopouts() {
  const poppedOut = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const isPoppedOut = useCallback((slot: string) => poppedOut.has(slot), [poppedOut])
  return {
    /** Reactive set of slot keys currently open in a popout window. */
    poppedOut,
    isPoppedOut,
    /** True when THIS window is itself the popout for the slot (stable per window lifetime). */
    isSelfPopout,
    /** Open a session in its own window (focuses the existing one if already out). */
    open: openPopout,
    /** Focus the existing popout window for a session. */
    focus: focusPopout,
    /** Close a session's popout window (caller re-selects it in the main window). */
    bringBack,
    /** From inside a popout: return this window's session to the main dashboard. */
    returnSelfToMain,
  }
}
