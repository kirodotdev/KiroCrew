/** While the person can see a ChatEmbed, its rows count as read, watermarked at the newest
 *  gateway-minted row it renders; a hidden, collapsed or scrolled-away embed reports nothing. */
import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import type { ChatMessage } from '../types'
import { emitExactSlotRead, flushSlotRead, newestServerRowTs } from '../lib/slotReadRelay'
import { isWindowAway } from '../hooks/windowAway'
import { elementOnScreen } from '../hooks/paneVisibility'

/** Whether the person can see *el* now. */
function embedSeen(el: HTMLElement | null, hostActive: boolean): boolean {
  if (!el || !hostActive || isWindowAway() || !elementOnScreen(el)) return false
  if (typeof el.checkVisibility === 'function') return el.checkVisibility({ visibilityProperty: true, opacityProperty: true })
  if (!el.isConnected || el.getClientRects().length === 0 || getComputedStyle(el).visibility === 'hidden') return false
  for (let node: Element | null = el; node; node = node.parentElement) if (getComputedStyle(node).opacity === '0') return false
  return true
}

/** Relay a read of *rows* for *slotKey* while the transcript under *scrollerRef* is seen. */
export function useEmbedSlotRead(
  slotKey: string,
  rows: readonly ChatMessage[],
  readAt: number,
  hostActive: boolean,
  scrollerRef: RefObject<HTMLElement | null>,
): void {
  const [seen, setSeen] = useState(false)
  const lastReadRef = useRef<{ slotKey: string; readTs: string } | undefined>(undefined)
  useEffect(() => {
    const check = () => setSeen(embedSeen(scrollerRef.current, hostActive))
    check()
    document.addEventListener('visibilitychange', check)
    window.addEventListener('focus', check)
    window.addEventListener('blur', check)
    return () => {
      document.removeEventListener('visibilitychange', check)
      window.removeEventListener('focus', check)
      window.removeEventListener('blur', check)
    }
  }, [hostActive, scrollerRef])
  const readTs = useMemo(() => newestServerRowTs(rows), [rows])
  useEffect(() => {
    if (!readTs || (lastReadRef.current?.slotKey === slotKey && lastReadRef.current.readTs === readTs)) return
    if (!embedSeen(scrollerRef.current, hostActive)) return
    emitExactSlotRead(slotKey, readTs)
    lastReadRef.current = { slotKey, readTs }
    // A read proves the embed is seen, so a later blur or hide still flushes its trailing read.
    setSeen(true)
  }, [slotKey, readTs, readAt, seen, hostActive, scrollerRef])
  useEffect(() => {
    if (!seen) return
    // The embed stops being seen: its trailing read goes now, so it cannot cover a later row.
    return () => flushSlotRead(slotKey)
  }, [slotKey, seen])
}
