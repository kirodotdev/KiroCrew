import { useState, useRef, useEffect, useId, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import { Info } from 'lucide-react'

import { i18nT } from '../i18n/t'

/**
 * Small info glyph that reveals a help tip.
 *
 * One pattern for every surface, so a reader learns it once:
 *   - mouse:    resting on the glyph for a short delay shows the tip, which
 *               then holds while the pointer is on the glyph or the bubble;
 *               leaving both hides it after an equal grace. A pointer that
 *               merely crosses the glyph never opens it;
 *   - keyboard: focus shows it at once; while open, the arrow and Page keys
 *               (and Home / End) scroll a long tip without moving focus off
 *               the glyph; Escape or blur hides it;
 *   - touch:    a tap pins it, another tap (or a tap elsewhere) unpins it.
 * A click on the glyph pins on any input. A pointer down inside the bubble
 * pins it too, and a click there goes no further than the bubble (never to the
 * row beneath), so selecting and copying text keeps the tip open however it
 * was opened -- even when the drag runs past the bubble's edge. A pinned tip
 * closes on a pointer down outside it, focus leaving it, or Escape pressed
 * anywhere. There is no dismissal latch: every close
 * clears the hover and focus reveals, and the next real hover or focus shows
 * the tip again. While the tip is shown it is the glyph's accessible
 * DESCRIPTION (`aria-describedby`); while hidden, the same text sits on
 * `title`, the native fallback that is also what assistive technology falls
 * back to for a description -- so the help is attached whether or not the tip
 * is open, and nothing of it is in the body copy until it is. The `title` is
 * dropped the moment the tip shows, so the browser's own tooltip never doubles
 * it. Portal-rendered to escape overflow clipping.
 */
/** How long the tip survives after the pointer leaves the glyph or the bubble. */
export const HOVER_LEAVE_GRACE_MS = 150
/**
 * How long a mouse must rest on the glyph before hover opens the tip. The
 * bubble is an opaque overlay that settings rows place ABOVE the glyph, over
 * the previous row, and a click on it goes no further than the bubble -- so a
 * pointer passing through the glyph on its way to that row must not open one
 * that then intercepts the click. Mirrors the leave grace.
 */
export const HOVER_OPEN_DELAY_MS = 150

export default function InfoTip({ text, placement = 'auto' }: {
  text: string
  placement?: 'auto' | 'top'
}) {
  const [pinned, setPinned] = useState(false)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  // Hover ends after a short grace rather than on the leave event itself, so a
  // pointer that skims off the glyph and back does not flicker the tip, and
  // the bubble stays hovered across the 6px gap between glyph and bubble.
  // Entering either element cancels the pending close.
  const leaveTimer = useRef<number | null>(null)
  const cancelLeave = () => {
    if (leaveTimer.current !== null) { window.clearTimeout(leaveTimer.current); leaveTimer.current = null }
  }
  const scheduleLeave = () => {
    cancelLeave()
    leaveTimer.current = window.setTimeout(() => {
      leaveTimer.current = null
      setHovered(false)
    }, HOVER_LEAVE_GRACE_MS)
  }
  // Hover on the GLYPH starts after a short delay rather than on the enter
  // event itself: the bubble is opaque and may sit over the row above, so a
  // pointer crossing the glyph on its way there must not open one that then
  // takes the click. The glyph's leave cancels a pending open. The bubble's
  // own enter is not delayed -- it only fires once the tip is open and must
  // keep asserting hover at once (see the bubble's onPointerEnter).
  const openTimer = useRef<number | null>(null)
  const cancelOpen = () => {
    if (openTimer.current !== null) { window.clearTimeout(openTimer.current); openTimer.current = null }
  }
  const scheduleOpen = () => {
    if (openTimer.current !== null) return
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null
      setHovered(true)
    }, HOVER_OPEN_DELAY_MS)
  }
  // A selection drag that leaves the bubble with a button held schedules
  // nothing on leave (see the bubble's onPointerLeave); instead the close is
  // scheduled once the button is released anywhere, so the tip does not stay
  // open forever after a drag that started outside it. Re-entering the bubble
  // before the release withdraws that pending close.
  const releaseListener = useRef<(() => void) | null>(null)
  const cancelReleaseClose = () => {
    if (releaseListener.current) { window.removeEventListener('pointerup', releaseListener.current); releaseListener.current = null }
  }
  const scheduleLeaveOnRelease = () => {
    cancelReleaseClose()
    const fn = () => { releaseListener.current = null; scheduleLeave() }
    releaseListener.current = fn
    window.addEventListener('pointerup', fn, { once: true })
  }
  useEffect(() => () => { cancelOpen(); cancelLeave(); cancelReleaseClose() }, [])
  const tipId = useId()
  const open = pinned || hovered || focused
  // Every way of closing the tip clears all three reveals at once, so no
  // dismissal has to be remembered: the next real hover or focus re-opens it.
  // Clearing `focused` while the glyph still has focus is self-correcting --
  // the next onFocus sets it again.

  // Escape closes the tip from wherever focus is, for as long as it is OPEN,
  // not only while pinned. The trigger's own Escape handler needs the glyph
  // focused, and a hover-opened bubble never focuses it -- yet that bubble is
  // an opaque overlay across the rows beneath, and content shown on hover
  // must be dismissible without moving the pointer (WCAG 2.1 SC 1.4.13).
  // A pinned tip also outlives the trigger's focus by design (a reader clicks
  // into the bubble to copy text, which blurs the glyph), so the same
  // window capture listener covers it once the glyph is no longer focused,
  // claiming Escape before Radix's document capture listener can dismiss a dialog.
  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Claim the key before closing: an enclosing Modal's window Escape
      // listener checks `defaultPrevented`, so this Escape closes the tip
      // only, not the dialog around it.
      e.preventDefault()
      e.stopPropagation()
      cancelOpen()
      setPinned(false); setHovered(false); setFocused(false)
    }
    window.addEventListener('keydown', onKeyDown, { capture: true })
    return () => window.removeEventListener('keydown', onKeyDown, { capture: true })
  }, [open])

  // The pointer and focus dismissals are for a PINNED tip only: a pointer
  // down outside it, or focus moving outside it (Tab onward). A hover- or
  // focus-opened tip already closes on its own leave or blur, so these would
  // only fight the reveal that holds it open.
  useEffect(() => {
    if (!pinned) return
    const outside = (target: EventTarget | null) =>
      !btnRef.current?.contains(target as Node) && !tipRef.current?.contains(target as Node)
    const onMouseDown = (e: MouseEvent) => { if (outside(e.target)) setPinned(false) }
    const onFocusIn = (e: FocusEvent) => { if (outside(e.target)) setPinned(false) }
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('focusin', onFocusIn)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('focusin', onFocusIn)
    }
  }, [pinned])

  /**
   * Where the bubble goes, decided from the glyph's rect alone -- no second
   * paint, no measurement. The bubble's own height is unknown at this point
   * and can be large (a paragraph moved out of a row's body copy), so the
   * side is chosen by ROOM rather than by a nominal height: anchor the bubble
   * to whichever vertical side of the glyph has more space and cap its
   * `maxHeight` to that space, and anything longer scrolls inside it. A
   * `position: fixed` element that ran past the viewport edge could never be
   * scrolled to, which is what this prevents.
   */
  const pos = (): CSSProperties => {
    if (!btnRef.current) return { top: 0, left: 0 }
    const r = btnRef.current.getBoundingClientRect()
    const tipW = 300
    const vh = window.innerHeight
    // The bubble's ceiling: 60% of the viewport, at most 480px, and never
    // more than the space on the chosen side less the 8px margin.
    const cap = Math.min(480, Math.round(vh * 0.6))
    if (placement === 'top') {
      // Centered above the button, bottom-anchored so height doesn't matter.
      let left = r.left + r.width / 2 - tipW / 2
      left = Math.max(8, Math.min(left, window.innerWidth - tipW - 8))
      const above = r.top - 6 - 8
      const below = vh - r.bottom - 6 - 8
      const MIN_ROOM = 120 // enough for ~5 lines of the 300px-wide tip
      // The caller asked for above (the bubble is moved off the content below
      // the glyph on purpose); fall below only when above cannot fit a
      // readable bubble AND below has more room.
      if (above >= MIN_ROOM || above >= below) return { bottom: vh - r.top + 6, left, maxHeight: Math.min(cap, above) }
      return { top: r.bottom + 6, left, maxHeight: Math.min(cap, below) }
    }
    let left = r.right + 6
    if (left + tipW > window.innerWidth) left = r.left - tipW - 6
    // The left-flip can land past the left edge when the button sits within
    // tipW of it (any narrow viewport); clamp to the same 8px margins the
    // 'top' branch uses so the tip always stays readable on-screen.
    left = Math.max(8, Math.min(left, window.innerWidth - tipW - 8))
    // Beside the glyph: top-aligned with it when the space below reaches the
    // cap, otherwise bottom-aligned with it so a tall bubble grows upward.
    const below = vh - r.top - 8
    if (below >= cap) return { top: r.top, left, maxHeight: cap }
    const above = r.bottom - 8
    if (above > below) return { bottom: Math.max(8, vh - r.bottom), left, maxHeight: Math.min(cap, above) }
    return { top: r.top, left, maxHeight: Math.max(0, below) }
  }

  return (
    <>
      <button
        ref={btnRef}
        /* A <button> defaults to type="submit": inside a <form> (the New crewmate
           dialog wraps its fields in one) a bare toggle would submit the owner form
           on click. This is a help toggle, never a submitter. */
        type="button"
        /* The glyph alone would be announced as "info" or nothing -- a control with
           no discoverable purpose. The NAME is a short generic verb phrase and the
           tip text is attached as the DESCRIPTION instead, which matters for two
           reasons: a name is read whenever the button is reached, so a
           paragraph-length one is unusable; and an accessible name is the handle
           every other control is found by, so naming a help toggle after its own
           prose makes it collide with real actions whose labels happen to appear
           in that prose. */
        aria-label={i18nT('components.infoTip.more_information')}
        aria-expanded={open}
        aria-describedby={open ? tipId : undefined}
        title={open ? undefined : text}
        /* Unpinning also clears the hover and focus reveals: the click that
           unpins leaves the trigger focused (and, on a mouse, hovered), so
           without this the tip would stay open and a second tap could never
           close it. */
        onClick={(e) => {
          e.stopPropagation()
          const next = !pinned
          setPinned(next)
          if (!next) { cancelOpen(); setHovered(false); setFocused(false) }
        }}
        /* Hover is a mouse affordance only. A finger has no hover, and the
           synthetic enter a tap fires would race the click that pins. A mouse
           entering cancels any pending close (the pointer came back) and starts
           the open delay; leaving withdraws a pending open before scheduling
           the close, so a pass-through never shows the bubble. */
        onPointerEnter={(e) => { if (e.pointerType === 'mouse') { cancelLeave(); scheduleOpen() } }}
        onPointerLeave={() => { cancelOpen(); scheduleLeave() }}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={(e) => {
          if (!open) return
          if (e.key === 'Escape') {
            e.stopPropagation()
            cancelOpen()
            setPinned(false); setHovered(false); setFocused(false)
            return
          }
          /* The bubble is capped to the room on its side of the glyph and
             scrolls, but it is not a tab stop (moving focus to it would blur
             the glyph and end the focus reveal). So while the tip is open the
             scroll keys act on the bubble from the glyph: arrows by a line,
             Page keys by a view, Home / End to either end. Closed, the keys
             fall through to the page as usual. `scrollTop` is assigned rather
             than `scrollBy` called, so the step works wherever the element is
             scrollable at all. */
          const tip = tipRef.current
          if (!tip) return
          const LINE = 24
          const view = tip.clientHeight
          const max = tip.scrollHeight - view
          let next: number | null = null
          switch (e.key) {
            case 'ArrowDown': next = tip.scrollTop + LINE; break
            case 'ArrowUp': next = tip.scrollTop - LINE; break
            case 'PageDown': next = tip.scrollTop + view; break
            case 'PageUp': next = tip.scrollTop - view; break
            case 'Home': next = 0; break
            case 'End': next = max; break
          }
          if (next === null) return
          e.preventDefault()
          tip.scrollTop = Math.max(0, Math.min(next, max))
        }}
        className={`w-4 h-4 rounded-full transition-colors leading-none cursor-pointer flex items-center justify-center shrink-0 ${open ? 'text-text' : 'text-muted hover:text-text'}`}
      >
        <Info size={14} aria-hidden="true" />
      </button>
      {open && createPortal(
        // Swallow clicks inside the bubble so they never reach the owning row
        // through React's portal ancestry (SettingsToggle). Selecting and
        // copying text must not close the tip, and the bubble asserts that
        // itself rather than relying on whichever reveal opened it: a pointer
        // down inside pins it (the mousedown that follows blurs the glyph, which
        // would otherwise end a focus-only reveal), and a pointer entering it
        // counts as hover. Dismissal remains hover-leave, outside mousedown
        // while pinned, focus leaving, or Escape.
        // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
        <div
          ref={tipRef}
          id={tipId}
          role="tooltip"
          onClick={(e) => e.stopPropagation()}
          onPointerDown={() => setPinned(true)}
          /* The bubble is part of the hover target, pinned or not: entering it
             cancels the close the glyph's leave scheduled and holds the hover
             reveal in its own right; leaving it schedules one, so a pointer can
             cross onto a long tip to read it. A leave with a button still held
             is a selection drag running past the bubble's edge, not the reader
             moving on: scheduling the close there would unmount the bubble
             mid-drag and destroy the selection, so the close waits for the
             button's release instead (withdrawn if the pointer comes back). */
          onPointerEnter={(e) => { if (e.pointerType === 'mouse') { cancelLeave(); cancelReleaseClose(); setHovered(true) } }}
          onPointerLeave={(e) => { if (e.buttons === 0) scheduleLeave(); else scheduleLeaveOnRelease() }}
          className="fixed z-[9999] rounded-lg border border-border p-2.5 text-[12px] text-muted leading-relaxed max-w-[300px] max-h-[min(60vh,480px)] overflow-y-auto whitespace-normal"
          style={{ ...pos(), backgroundColor: 'var(--card)', boxShadow: '0 4px 24px rgba(0,0,0,0.5)' }}
        >
          {text}
        </div>,
        document.body
      )}
    </>
  )
}
