import { useEffect, type RefObject } from 'react'
import { FRAGMENT_SCROLL_MESSAGE } from '../lib/widgetFragmentScroll'

/** The nearest ancestor that scrolls vertically, or null for the page itself. */
function scrollParentOf(el: HTMLElement): HTMLElement | null {
  for (let n = el.parentElement; n; n = n.parentElement) {
    const y = getComputedStyle(n).overflowY
    if ((y === 'auto' || y === 'scroll' || y === 'overlay') && n.scrollHeight > n.clientHeight) return n
  }
  return null
}

/**
 * Scrolls the host to an in-page link target inside a frame sized to its own
 * document, which cannot scroll to it itself (lib/widgetFragmentScroll.ts).
 *
 * Source-checked like the height report: the document is model- or
 * user-authored and its scripts can post anything, so only THIS frame's window
 * is heard. The offset is clamped to the frame's own height, so a page can move
 * the host only to somewhere inside the artifact, which the reader could scroll
 * to anyway. Pass `enabled: false` for a frame held at a fixed height: it
 * scrolls itself.
 */
export function useFrameFragmentScroll(iframeRef: RefObject<HTMLIFrameElement | null>, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return
    function onMessage(e: MessageEvent) {
      const frame = iframeRef.current
      if (!frame || e.source !== frame.contentWindow) return
      if (e.data?.type !== FRAGMENT_SCROLL_MESSAGE) return
      const raw = e.data.top
      if (typeof raw !== 'number' || !Number.isFinite(raw)) return
      const top = Math.min(Math.max(raw, 0), frame.clientHeight)
      const scroller = scrollParentOf(frame)
      const viewTop = scroller ? scroller.getBoundingClientRect().top : 0
      const delta = frame.getBoundingClientRect().top + top - viewTop
      if (scroller) scroller.scrollTop += delta
      else window.scrollTo({ top: window.scrollY + delta })
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [iframeRef, enabled])
}
