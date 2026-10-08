import { useLayoutEffect, useRef, type RefObject } from 'react'

/** The two reads of the transcript handle this hook needs. */
export interface FollowHandle {
  getFollow: () => boolean
  scrollToBottom: (behavior?: ScrollBehavior) => void
}

/**
 * Keep a following reader at the end when Mate's held status line resolves.
 *
 * While a turn is live Mate's first text draws as ONE muted line
 * (`mateNarration` → `status`), so the reply grows by nothing until the line
 * resolves, and it resolves when the run ends: the whole answer's height lands
 * in the commit that also takes the working indicator away. The follow core
 * judges that growth as idle (`runActive` false), and the indicator's removal
 * has just clamped the scroll position off the core's last write, so it reads
 * the reader as one who left and releases follow: the answer stops half under
 * the composer. A reader who was following while the line was held is
 * re-pinned once it resolves; a reader who had scrolled up is left alone.
 */
export function useCarryHeldAnswer(heldStatus: boolean, listRef: RefObject<FollowHandle | null>): void {
  const followingRef = useRef(false)
  useLayoutEffect(() => {
    if (heldStatus) {
      followingRef.current = listRef.current?.getFollow() ?? false
      return
    }
    if (!followingRef.current) return
    followingRef.current = false
    listRef.current?.scrollToBottom('auto')
  })
}
