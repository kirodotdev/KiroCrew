import { useSyncExternalStore } from 'react'

/**
 * Whether the chat page's sessions list is on screen, published for the shell
 * (shell/appShell.tsx), which holds the rail compact beside a fold only while a
 * list sits there. The one source is `sidebarOnScreen` in
 * pages/chat/page/sessionsDrawer.ts; it publishes from a layout effect, so the
 * shell reads the current value before paint, and resets to false when it
 * unmounts, so leaving the chat page never leaves a stale hold.
 */
let onScreen = false
const listeners = new Set<() => void>()

/** Called by the sessions drawer. No-op when the value is unchanged. */
export function setSessionsListOnScreen(next: boolean) {
  if (next === onScreen) return
  onScreen = next
  listeners.forEach(l => l())
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

const getSnapshot = () => onScreen

export function useSessionsListOnScreen() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** Test seam: restore the module default between cases. */
export function __resetSessionsListOnScreen() {
  onScreen = false
  listeners.clear()
}
