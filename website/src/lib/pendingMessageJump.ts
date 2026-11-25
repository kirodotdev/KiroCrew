/** A request to scroll a chat to one message once that chat is on screen.
 *
 *  Set by a surface that opens a chat from outside the chat page (Search all
 *  chats), and taken by the chat page's transcript jumps once the named slot is
 *  active and its window belongs to it. The page then pages back to the message
 *  with the same jump the pins and `?msg=` deep links use, and highlights it.
 *  In memory only: a reload drops a jump that was never taken. */
import { useSyncExternalStore } from 'react'

export interface MessageJumpTarget {
  /** The slot key the resume returned, which the chat page compares with its active slot. */
  slotKey: string
  /** The message's transcript `ts`. */
  ts: string
  /** The message's `meta.mid`, which tells apart two messages of one tick. */
  mid?: string
}

const listeners = new Set<() => void>()
let current: MessageJumpTarget | null = null

function emit() {
  for (const listener of listeners) listener()
}

export function requestMessageJump(target: MessageJumpTarget): void {
  current = { ...target }
  emit()
}

/** Clears the request when it is still *target*; a newer request is kept. */
export function clearMessageJump(target: MessageJumpTarget | null): void {
  if (target === null || current === target) {
    current = null
    emit()
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

function snapshot() {
  return current
}

export function usePendingMessageJump(): MessageJumpTarget | null {
  return useSyncExternalStore(subscribe, snapshot, snapshot)
}
