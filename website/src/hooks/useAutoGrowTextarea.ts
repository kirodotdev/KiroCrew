import { useLayoutEffect } from 'react'
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
export function useAutoGrowTextarea(
  ref: RefObject<HTMLTextAreaElement | null>,
  value: string,
  maxH = 200,
): void {
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, maxH)}px`
    el.style.overflowY = el.scrollHeight > maxH ? 'auto' : 'hidden'
  }, [ref, value, maxH])
}
