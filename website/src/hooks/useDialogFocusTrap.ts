// Shared modal-dialog keyboard behaviour: focus in on mount, focus back out on
// unmount, Escape to dismiss, and Tab/Shift+Tab cycling WITHIN the dialog.
//
// An aria-modal dialog that lets focus wander behind the overlay would let
// keyboard users activate obscured controls, so every dialog needs the same
// three pieces. They live here rather than being re-implemented per dialog.
import { useEffect, useRef, type RefObject } from 'react'
import { createImeLatch } from './useImeGuard'

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
 * `handleEscape` may be disabled when a caller deliberately owns Escape on a
 * bubble-phase listener. Modal uses that path so a nested overlay can consume
 * Escape before the outer dialog observes it, while this hook still owns focus
 * entry, restoration, and Tab trapping.
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
  handleEscape = true,
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

  // IME guard for the Tab-cycles-focus path. This listener receives NATIVE
  // KeyboardEvents (window capture), which `useImeGuard`'s synthetic-only
  // `claimEnter` cannot consume — so it shares the guard's tracked latch via
  // `createImeLatch` instead of hand-rolling a second spelling
  // (`useListKeyboardNav` is the reference consumer). IMEs use Tab to cycle
  // the candidate list, and on WebKit the keydown that commits a candidate
  // arrives AFTER `compositionend` with `isComposing` already false —
  // unguarded, a Tab composed into a dialog input that happens to be the
  // last (or first) focusable element yanks focus and aborts the
  // composition. The latch lives in a ref so the keydown handler reads the
  // live value without re-subscribing.
  const imeLatchRef = useRef<ReturnType<typeof createImeLatch>>()
  if (!imeLatchRef.current) imeLatchRef.current = createImeLatch()

  // Composition tracking + latch lifecycle, keyed on `enabled` ONLY. The
  // keydown effect below re-attaches whenever a dep changes identity (callers
  // routinely pass an inline `onEscape`), and a latch reset riding along on
  // that churn would clear the post-composition window at exactly the moment
  // it matters: the commit's own input event re-renders the host right before
  // the committing keydown arrives. Same constraint `useListKeyboardNav`
  // states for its guard.
  useEffect(() => {
    if (!enabled) return
    const latch = imeLatchRef.current!
    // A re-enabled trap must not inherit a latch stranded by a composition
    // that ended (or was abandoned) while an inner dialog owned the keyboard.
    latch.reset()
    // Composition tracking listens at document capture like
    // `useListKeyboardNav`'s: the composing input is anywhere inside the
    // dialog, so element-scoped listeners have nothing reliable to attach to.
    const onCompositionStart = () => latch.onCompositionStart()
    const onCompositionEnd = () => latch.onCompositionEnd()
    // Stranded-latch recovery: a composition abandoned WITHOUT compositionend
    // (focus moves off the input mid-composition, an OS-level IME cancel)
    // would otherwise leave the latch set — and a latched guard consumes the
    // Tabs it declines, so the trap would silently stop cycling for the
    // dialog's lifetime.
    const onRecover = () => latch.reset()
    document.addEventListener('compositionstart', onCompositionStart, true)
    document.addEventListener('compositionend', onCompositionEnd, true)
    document.addEventListener('focusout', onRecover, true)
    return () => {
      document.removeEventListener('compositionstart', onCompositionStart, true)
      document.removeEventListener('compositionend', onCompositionEnd, true)
      document.removeEventListener('focusout', onRecover, true)
      // Drop any pending post-composition timer with the listeners so a timer
      // from a disabled trap cannot fire into the next enablement.
      latch.reset()
    }
  }, [enabled])

  useEffect(() => {
    if (!enabled) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && handleEscape) {
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
      const wrapsForward = !e.shiftKey && active === lastEl
      const wrapsBackward = e.shiftKey && (active === firstEl || active === container)
      const refocuses = !wrapsForward && !wrapsBackward && !container.contains(active)
      // A mid-dialog Tab is the browser's to move, not the trap's — so it is
      // also not the trap's to claim: claiming it would consume legitimate
      // navigation inside the post-composition latch window.
      if (!wrapsForward && !wrapsBackward && !refocuses) return
      // A Tab the IME owns must not cycle focus — the user is choosing a
      // candidate, not leaving the field. `claimKey` owns the whole decline
      // (stopPropagation always; preventDefault only in the post-composition
      // latch window where the browser would otherwise act) — see its
      // contract in useImeGuard.ts. It must run before the preventDefault()
      // and focus move so the IME keeps the key.
      if (!imeLatchRef.current!.claimKey(e)) return
      e.preventDefault()
      ;(wrapsBackward ? lastEl : firstEl).focus()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [containerRef, onEscape, enabled, handleEscape])
}
