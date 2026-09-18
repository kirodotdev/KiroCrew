import { useCallback, useRef, useState } from 'react'
import { useAnchorRemeasure } from './useAnchorRemeasure'

/**
 * Anchor state for a portaled menu whose trigger lives in a CHILD component.
 *
 * The composer-toolbar pickers (agent, model, reasoning effort, project,
 * app session controls) keep their open/closed state and their menu portal in
 * the host page (ChatPage / ChatPane), while the chip that opens them is a
 * button inside ChatInput. The chip's click handler hands the host its rect,
 * and the host used to store that one-time snapshot as the menu's anchor --
 * so anything that moved the composer while the menu stayed open (the mobile
 * keyboard closing, the composer growing, a scroll) left the menu floating
 * where the chip used to be (#10616, same class as #10580).
 *
 * This hook owns that anchor. `anchorTo(rect, trigger)` records the click
 * snapshot AND the trigger element; while `open`, `useAnchorRemeasure` re-reads
 * the trigger's live rect on every resize / scroll / visual-viewport change so
 * the menu follows it. A caller with no element to hand over (a fallback rect
 * for a chip that is not on screen) still gets the snapshot; there is simply
 * nothing to remeasure against.
 *
 * `rect` is null until the first `anchorTo`, so hosts can keep gating the
 * portal on it exactly as they gated the old `useState<DOMRect | null>`.
 */
export function useAnchoredTriggerRect(open: boolean): {
  rect: DOMRect | null
  anchorTo: (rect: DOMRect, trigger?: HTMLElement | null) => void
} {
  const triggerRef = useRef<HTMLElement | null>(null)
  const [rect, setRect] = useState<DOMRect | null>(null)
  const measure = useCallback(() => {
    const el = triggerRef.current
    // A trigger that left the DOM while the menu was open (a shelf re-layout
    // swapped the chip) measures as an all-zero rect; keep the last good one.
    if (el?.isConnected) setRect(el.getBoundingClientRect())
  }, [])
  useAnchorRemeasure(open, measure)
  const anchorTo = useCallback((next: DOMRect, trigger?: HTMLElement | null) => {
    triggerRef.current = trigger ?? null
    setRect(next)
  }, [])
  return { rect, anchorTo }
}
