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
  // composer inside a hidden pane; IntersectionObserver here because visibility
  // is what changes, and `measure` sets nothing that observer reads.
  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) measure(el, maxH)
    })
    io.observe(el)
    return () => io.disconnect()
  }, [ref, maxH])

  // Re-measure when the box's WIDTH changes at an unchanged value (a window
  // resize, a pane folding beside it): the text wraps at a different column and
  // the value-keyed effect above cannot know. Width only -- `measure` writes the
  // height, which this observer also sees, and re-measuring on that would loop;
  // the guard makes a height-only notification a no-op.
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    let lastWidth = el.clientWidth
    const ro = new ResizeObserver(() => {
      const width = el.clientWidth
      if (width === lastWidth) return
      lastWidth = width
      measure(el, maxH)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref, maxH])
}
