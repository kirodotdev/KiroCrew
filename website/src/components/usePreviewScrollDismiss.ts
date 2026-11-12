import { useEffect, useLayoutEffect, useRef } from 'react'

/**
 * Dismiss a viewport-anchored paste preview on resize, and on a scroll only
 * when that scroll moved its anchor element. The preview is position:fixed at
 * the anchor's rect, so a scroll that moved the anchor would strand it; a
 * scroll in another pane, or one that ends where it began, leaves the anchor
 * in place and so leaves the preview open. Same rule as the chat link-preview
 * layer (LinkPreviewLayer).
 *
 * The position is checked twice: at once, and again on the next frame. A
 * capture-phase window listener runs before the scrolled element's own
 * handlers, and the composer's mirror div (the anchor of its token previews)
 * only follows the textarea's scroll in that handler.
 */
export function usePreviewScrollDismiss(
  open: boolean,
  anchorEl: () => Element | null | undefined,
  close: () => void,
): void {
  const anchorRef = useRef(anchorEl)
  const closeRef = useRef(close)
  useLayoutEffect(() => {
    anchorRef.current = anchorEl
    closeRef.current = close
  })

  useEffect(() => {
    if (!open) return
    const start = anchorRef.current()?.getBoundingClientRect()
    let frame: number | null = null
    const moved = () => {
      const el = anchorRef.current()
      if (!start || !el || !el.isConnected) return true
      const r = el.getBoundingClientRect()
      return Math.abs(r.left - start.left) >= 1 || Math.abs(r.top - start.top) >= 1
    }
    const onScroll = () => {
      if (moved()) { closeRef.current(); return }
      if (frame !== null) return
      frame = requestAnimationFrame(() => {
        frame = null
        if (moved()) closeRef.current()
      })
    }
    const onResize = () => closeRef.current()
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => {
      if (frame !== null) cancelAnimationFrame(frame)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
    }
  }, [open])
}
