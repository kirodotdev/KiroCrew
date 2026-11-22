/** Whether the Search all chats dialog is open.
 *
 *  A module store rather than Redux state so its openers (the ⌘⇧F shortcut, the
 *  sidebar button) need no dispatch and the dialog mounted once in the shell
 *  reads one flag. */
import { useSyncExternalStore } from 'react'

const listeners = new Set<() => void>()
let open = false

function set(next: boolean) {
  if (open === next) return
  open = next
  for (const listener of listeners) listener()
}

export function openGlobalChatSearch(): void {
  set(true)
}

export function closeGlobalChatSearch(): void {
  set(false)
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

function snapshot() {
  return open
}

export function useGlobalChatSearchOpen(): boolean {
  return useSyncExternalStore(subscribe, snapshot, snapshot)
}
