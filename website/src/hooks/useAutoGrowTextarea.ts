import { useEffect, useLayoutEffect } from 'react'
import type { RefObject } from 'react'

/**
 * Auto-grow a controlled <textarea> with its content: the box expands as the
 * user types and shrinks when text is removed, capping at `maxH` (after which
 * it scrolls). Re-runs whenever `value` changes, so a programmatic clear (e.g.
 * after submitting a comment) resets the height too. Mirrors the proven resize
 * pattern used by ChatInput / CommentOverlay so behavior is consistent.
 *
 * The textarea should keep `resize-none`; an initial `rows` attribute sets the
 * resting height before the first measure (avoids a paint flash).
 */
function measure(el: HTMLTextAreaElement, maxH: number): void {
  // An element inside a hidden pane has no layout box, so `scrollHeight` reads 0.
  // Writing that back as an explicit height leaves a sliver -- the padding and
  // border around a zero-height content box -- and the value-keyed effect below
  // cannot recover it, because becoming visible is not a value change.
  if (el.scrollHeight === 0 || !el.offsetParent) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, maxH)}px`
  el.style.overflowY = el.scrollHeight > maxH ? 'auto' : 'hidden'
}

export function useAutoGrowTextarea(
  ref: RefObject<HTMLTextAreaElement | null>,
  value: string,
  maxH = 200,
): void {
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    measure(el, maxH)
  }, [ref, value, maxH])

  // Re-measure once the field gains a layout box. A responsive shell may mount a
  // composer inside a hidden pane; IntersectionObserver and not ResizeObserver,
  // because `measure` SETS the height it would otherwise observe.
  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) measure(el, maxH)
    })
    io.observe(el)
    return () => io.disconnect()
  }, [ref, maxH])
}
