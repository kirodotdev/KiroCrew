import { createContext, useContext } from 'react'

import type { ChatMessage } from '../types'

/**
 * The `ts` of the message a jump just highlighted (a pin, a quote, a `?msg=`
 * link, Search all chats), or null. A turn whose step fold holds that message
 * opens the fold, as it does for an in-chat search match: a folded row is drawn
 * at zero height, so the reader would be scrolled to a highlight they cannot see.
 */
export interface JumpReveal {
  ts: string | null
  /** The jumped-to message's own id, when it has one: messages that share a `ts`
   *  (an import keeps the supplied stamps) are told apart by it. */
  mid: string | null
  /** Bumped by every jump, so a repeat jump to the same row reruns its reveal. */
  seq: number
  /** Ends the jump's own scroll, so the revealed row's scroll is not undone. */
  claimScroll: () => void
  /** The transcript scroller: a reader's own scroll on it cancels a pending row scroll. */
  scrollerEl: () => HTMLElement | null
}

/** No jump target: the default, and what a measuring-only render provides. */
export const NO_JUMP_REVEAL: JumpReveal = { ts: null, mid: null, seq: 0, claimScroll: () => {}, scrollerEl: () => null }

const JumpRevealContext = createContext<JumpReveal>(NO_JUMP_REVEAL)

export const useJumpReveal = () => useContext(JumpRevealContext)

/** Whether *m* is the message a jump marked: its `ts`, and its `mid` when the
 *  jump carried one, so only the chosen row of a same-`ts` run is marked. */
export function isJumpTarget(m: ChatMessage, ts: string | null, mid: string | null): boolean {
  if (!ts || m.ts !== ts) return false
  return !mid || (m.meta as Record<string, unknown> | undefined)?.mid === mid
}
export default JumpRevealContext
