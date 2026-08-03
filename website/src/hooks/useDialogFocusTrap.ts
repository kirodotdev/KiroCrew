// Shared modal-dialog keyboard behaviour: focus in on mount, focus back out on
// unmount, Escape to dismiss, and Tab/Shift+Tab cycling WITHIN the dialog.
//
// An aria-modal dialog that lets focus wander behind the overlay would let
// keyboard users activate obscured controls, so every dialog needs the same
// three pieces. They live here rather than being re-implemented per dialog.
import { useEffect, type RefObject } from 'react'

/** Focusable descendants of a dialog, in DOM order, skipping disabled ones. */
export const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'

/**
 * Wire a dialog element for modal keyboard use.
 *
 * `containerRef` must point at the dialog root (the `role="dialog"` element).
 * `onEscape` is called on Escape — the caller decides whether that actually
 * closes (e.g. a dialog with work in flight can refuse).
 *
 * `enabled` exists for stacked dialogs: a dialog that opens a dialog of its own
 * must stop trapping Tab, or focus is dragged back out of the inner one on every
 * keypress. Pass `false` for as long as an inner dialog owns the keyboard.
 * Only the key handling is gated — focus-in on mount and focus-restore on
 * unmount are deliberately NOT, because tying them to `enabled` would restore
 * focus to whatever was focused before the OUTER dialog opened (i.e. behind
 * both overlays) the moment the inner one appears.
 *
 * The keydown listener is CAPTURE phase on purpose: dialogs stop keydown
 * propagation so the page's own shortcuts don't fire while the user types
 * inside them, and a bubble-phase listener would then never see Escape or Tab
 * from inside the dialog — breaking both dismissal and the trap.
 */
export function useDialogFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  onEscape: () => void,
  enabled = true,
): void {
  // Move focus into the dialog on open, restore it on close, so keyboard users
  // aren't dumped at the top of the document afterwards.
  //
  // `preventScroll` on both halves matters: without it the browser scrolls every
  // scrollable ancestor to reveal the target. On open the dialog is still
  // mid-entrance (translated off-screen), so that scroll lands on the page
  // BEHIND it and the workspace visibly twitches as the animation settles; on
  // close the same happens to whatever regains focus.
  useEffect(() => {
    const restoreTo = document.activeElement as HTMLElement | null
    const first = containerRef.current?.querySelector<HTMLElement>(FOCUSABLE)
    ;(first ?? containerRef.current)?.focus({ preventScroll: true })
    return () => restoreTo?.focus?.({ preventScroll: true })
  }, [containerRef])

  useEffect(() => {
    if (!enabled) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onEscape()
        return
      }
      const container = containerRef.current
      if (e.key !== 'Tab' || !container) return
      const items = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter((el) => el.offsetParent !== null)
      if (!items.length) return
      const firstEl = items[0]
      const lastEl = items[items.length - 1]
      const active = document.activeElement
      if (!e.shiftKey && active === lastEl) {
        e.preventDefault()
        firstEl.focus()
      } else if (e.shiftKey && (active === firstEl || active === container)) {
        e.preventDefault()
        lastEl.focus()
      } else if (!container.contains(active)) {
        e.preventDefault()
        firstEl.focus()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [containerRef, onEscape, enabled])
}
