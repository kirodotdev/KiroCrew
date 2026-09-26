import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

/**
 * The shared instant-tooltip spelling for the follow-up chips and ChatInput's
 * `ResizeBadge`. `PastePreviewTooltip` records why this repo shares tooltip
 * renderers ("so the two previews cannot drift"); this module is the same rule
 * applied to the hover bubble those two call sites need. The chrome, the
 * positioning and the show/hide gesture live here once; a call site owns only
 * its content and a width/wrap variant class. (`ContextBreakdownPanel` still
 * hand-rolls a sibling hover tip; migrating it is deferred, tracked on
 * PR #9552's review thread.)
 *
 * Semantics, chosen against the native `title` this replaces:
 * - Pointer shows after a short intent delay (`OPEN_DELAY_MS`, 100ms) — long
 *   enough that a pointer merely crossing the element on its way somewhere
 *   else paints nothing, short enough to still read as instant. The ~1s OS
 *   delay was the defect; zero was the flicker.
 * - Keyboard focus shows synchronously. A tab stop is deliberate in a way a
 *   pointer transit is not, and a keyboard user has no second cursor to wave.
 * - Escape hides while open, without requiring blur.
 * - A scroll that can move the anchor hides while open: the position is
 *   captured at show time, so after such a scroll the bubble would sit
 *   detached from its anchor. Capture phase, because the strips that scroll
 *   (`overflow-x-auto`) do not bubble their scroll events to window. A scroll
 *   anywhere else in the document leaves the anchor where it was, so the
 *   bubble stays (see `scrollMovesAnchor`).
 * - A consumer may HOLD the bubble (`useInstantTip({ hold })`) while it carries
 *   an outcome rather than a hint — the copy chip's "Copied!" / "Copy failed"
 *   flash. While held, a mouse leave does not close it (a mouse user moves on
 *   right after clicking, and the bubble is the only visible confirmation),
 *   and a hold that begins with the bubble closed opens it at the last anchor
 *   (a click inside the intent window, or after the pointer already left,
 *   still owes its outcome). When the hold ends the bubble closes unless the
 *   pointer or focus is still on the anchor. Blur, Escape and an anchor-moving
 *   scroll close it regardless: a tab stop moved on deliberately, and a stale
 *   position is worse than a missed confirmation. One bubble at a time across
 *   anchors, held ones ranking above hints: a neighbour's hint arriving
 *   mid-flash yields and returns when the hold ends, a new outcome supersedes.
 */
export interface TipPos {
  top: number
  left: number
  /** Which side of the anchor the bubble opens on. Absent means `above`, the
   *  original and default: the bubble's bottom edge sits 8px over the anchor's
   *  top. `below` puts its top edge 8px under the anchor's bottom -- for an
   *  anchor in the top bar, where "above" is off-screen. */
  placement?: TipPlacement
}
export type TipPlacement = 'above' | 'below'

/** Hover-intent window. Long enough that a pointer merely crossing the anchor
 *  paints nothing, short enough to read as instant. A module constant, not a
 *  hook parameter: both consumers want the same feel, and a per-site knob was
 *  surface with zero callers. Exported so tests advance exactly this. */
export const OPEN_DELAY_MS = 100

/**
 * Whether a `scroll` event that fired on `target` can have moved `anchor` on
 * screen: the page itself scrolled (window / document), or a scroll container
 * the anchor sits inside scrolled. Any other element's scroll leaves the anchor
 * where it was, so the position captured at show time is still right.
 *
 * Fails closed: with no anchor to compare against, an anchor that is no longer
 * in the document (its element was replaced under the pointer while the bubble
 * stayed open -- a chip changing shape on a pick does that), or a target that
 * is not a DOM node, the scroll counts as moving it.
 */
export function scrollMovesAnchor(target: EventTarget | null, anchor: HTMLElement | null): boolean {
  if (!anchor || !anchor.isConnected || target === null || target === window || target === document) return true
  if (!(target instanceof Node)) return true
  return target !== anchor && target.contains(anchor)
}

/**
 * Every open bubble, so that opening one closes the rest. Each anchor owns its
 * own hook, and a HELD bubble ignores the mouse leave, so without this a pointer
 * moving from a confirming chip to its neighbour paints two portals at once — a
 * stale "Copied!" beside the new chip's hint, at the same `top`, overlapping for
 * chips on one line. One bubble at a time is also simply what a tooltip is.
 *
 * Held bubbles rank above hints. A hint arriving while another anchor's bubble
 * holds an OUTCOME yields instead of evicting it — the outcome is the only sign
 * a clipboard write landed or was refused, the hint is a hover cue the user gets
 * again by re-entering — and is resumed when that hold ends, if the pointer or
 * focus still rests on its anchor. A new outcome supersedes anything.
 */
type Bubble = { held: boolean; close: () => void; resume: () => void }
const openBubbles = new Set<Bubble>()
const yieldedHints = new Set<Bubble>()
/** Give the floor back to hints that yielded, once no held bubble remains open. */
function resumeYieldedHints() {
  for (const b of openBubbles) if (b.held) return
  for (const b of [...yieldedHints]) { yieldedHints.delete(b); b.resume() }
}

/**
 * `hold`: keep the bubble while it carries an OUTCOME rather than a hint (the
 * copy chip's "Copied!" / "Copy failed" flash). Truthy holds. Pass a value that
 * CHANGES with every new outcome — the copy chip hands over its attempt number —
 * not a boolean: a second refusal while the first still shows, or a refusal
 * inside the confirmation window, is a new outcome that must reopen a bubble a
 * scroll or Escape closed, and a boolean that stays `true` has no edge to do it
 * on. `arm(el)`: the user pressed `el`; a following outcome opens there even
 * when no enter or focus fires for that press (the pointer was already resting
 * on the chip after an Escape). `placement`: which side of the anchor the
 * bubble opens on (`TipPos.placement`).
 */
export function useInstantTip({ hold = 0, placement = 'above' }: { hold?: boolean | number; placement?: TipPlacement } = {}) {
  const [tip, setTip] = useState<TipPos | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const anchorRef = useRef<HTMLElement | null>(null)
  // The last element the user hovered, focused or pressed, kept across a hide
  // so a hold that starts with the bubble closed knows where to open it.
  const lastAnchorRef = useRef<HTMLElement | null>(null)
  // Whether the pointer is on the anchor right now, and whether keyboard focus
  // is — so the end of a hold can tell "the user is still here" from "the user
  // moved on". A mouse click focuses the anchor too, and that focus must NOT
  // count: a mouse user who clicked and moved the pointer away is done with the
  // chip, and a hint left floating over it would be the old leave-never-hides
  // bug in a new place. Focus that arrives with the pointer already on the
  // anchor is therefore recorded as pointer-driven; only focus that arrives
  // without it (a tab stop) holds the bubble open after the flash.
  const pointerInRef = useRef(false)
  const keyboardFocusRef = useRef(false)
  // Links the anchor to the bubble (`aria-describedby` -> `role="tooltip"`),
  // restoring what the native `title` gave screen readers for free. Applied
  // unconditionally: a described-by pointing at a not-yet-rendered id is
  // simply ignored, and a conditional one would re-announce on every show.
  const tipId = useId()

  const cancelPending = () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
  }
  // This hook's registry entry: one stable object whose fields are refreshed
  // every render, so another anchor's `showFor` reads the current hold and runs
  // the current `hide`.
  const entryRef = useRef<Bubble | null>(null)
  if (!entryRef.current) entryRef.current = { held: false, close: () => {}, resume: () => {} }
  const entry = entryRef.current
  entry.held = !!hold
  const holdRef = useRef(!!hold)
  holdRef.current = !!hold
  const showFor = (el: HTMLElement) => {
    if (!holdRef.current) {
      // A hint yields to a held outcome elsewhere; `resumeYieldedHints` brings
      // it back when that hold ends.
      for (const other of openBubbles) if (other !== entry && other.held) { yieldedHints.add(entry); return }
    }
    yieldedHints.delete(entry)
    // Registered BEFORE the others close: their `hide` looks for a held bubble
    // before resuming yielded hints, and a new outcome must be found there.
    openBubbles.add(entry)
    for (const other of openBubbles) if (other !== entry) other.close()
    if (placement === 'below') {
      // Under the anchor, from the BOUNDING box: the `below` consumer is a
      // single-line top-bar pill, and the fragment rule below exists for a
      // bubble opening above a wrapped inline anchor. No boundary lift either:
      // the lift exists so a bubble opening ABOVE a wrapped row does not cover
      // the row above it, which a bubble opening below cannot do.
      const b = el.getBoundingClientRect()
      setTip({ top: b.bottom + 8, left: b.left, placement })
      return
    }
    // The FIRST line fragment, not the bounding box. An inline anchor that
    // wraps — a long inline-code chip — has a bounding box whose top-left
    // corner belongs to no fragment: the top of line one at the left edge of
    // line two, so a bubble placed there floats over unrelated text. For a
    // block anchor, or an inline one on a single line, the two rects are equal.
    const r = el.getClientRects()[0] ?? el.getBoundingClientRect()
    // Lift above the nearest [data-tip-boundary] ancestor, when one exists.
    // In a wrapped chip row the anchor can sit in row 2+, and a bubble opening
    // just above IT covers the row above — the exact chips the user is
    // scanning. Lifting to the boundary's top opens the bubble above the whole
    // strip instead, where only the (transient-safe) message area sits. For a
    // first-row anchor, and for consumers without the attribute (ResizeBadge),
    // this is exactly the old position.
    const boundary = el.closest('[data-tip-boundary]')
    const top = boundary ? Math.min(r.top, boundary.getBoundingClientRect().top) : r.top
    setTip({ top: top - 8, left: r.left })
  }
  const hide = () => {
    openBubbles.delete(entry)
    yieldedHints.delete(entry)
    cancelPending()
    anchorRef.current = null
    setTip(null)
    resumeYieldedHints()
  }
  entry.close = hide
  // A yielded hint comes back only where the user still is: the pointer on the
  // anchor, or keyboard focus on it. Anywhere else the moment has passed.
  entry.resume = () => {
    const el = lastAnchorRef.current
    if ((pointerInRef.current || keyboardFocusRef.current) && el && el.isConnected) { anchorRef.current = el; showFor(el) }
  }
  // The user moved on deliberately (Escape, or focus left): an outcome that
  // settles afterwards must not bring the bubble back, so forget the anchor a
  // rising hold would reopen at. A mouse leave and an anchor-moving scroll keep
  // it: there the outcome still owes its bubble, at the recomputed position.
  const dismiss = () => { lastAnchorRef.current = null; hide() }
  const arm = (el: HTMLElement) => { lastAnchorRef.current = el }

  useEffect(() => () => {
    openBubbles.delete(entry)
    yieldedHints.delete(entry)
    cancelPending()
    resumeYieldedHints()
  }, [entry])

  // The hold's edges. A new (truthy) value with the bubble closed: open it at
  // the last anchor, which is where the user acted. Falling to none: close it,
  // unless the pointer or keyboard focus is still there — then it simply shows
  // the hint again; either way a hint that yielded to this hold gets its turn.
  useEffect(() => {
    if (hold) {
      const el = lastAnchorRef.current
      if (!tip && el && el.isConnected) { anchorRef.current = el; showFor(el) }
      return
    }
    if (tip && !pointerInRef.current && !keyboardFocusRef.current) hide()
    else resumeYieldedHints()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hold])

  // Escape and scroll dismiss only while open, so the listeners exist only
  // while open. The rect goes stale the moment the ANCHOR moves; hiding is
  // strictly better than a bubble stranded at old coordinates.
  //
  // Only a scroll that can move the anchor counts: the window/document, or a
  // scroll container the anchor sits inside. The capture-phase listener also
  // sees every other scroller in the document -- the transcript re-pinning
  // after a row re-measures, a sidebar lane re-sorting on a live update, a
  // side panel following its own tail -- none of which move a chip in the
  // composer band. Hiding on those reads as the bubble vanishing under a
  // resting pointer, for no reason the user can see.
  useEffect(() => {
    if (!tip) return
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') dismiss() }
    const onScroll = (e: Event) => {
      if (scrollMovesAnchor(e.target, anchorRef.current)) hide()
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('scroll', onScroll, { capture: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tip !== null])

  const tipHandlers = {
    'aria-describedby': tipId,
    onMouseEnter: (e: React.MouseEvent) => {
      cancelPending()
      const el = e.currentTarget as HTMLElement
      anchorRef.current = el
      lastAnchorRef.current = el
      pointerInRef.current = true
      // Rect is read when the timer fires, not at enter: the anchor can move
      // in the intent window (an entrance animation settling, a layout shift).
      timerRef.current = setTimeout(() => {
        timerRef.current = null
        if (anchorRef.current === el && el.isConnected) showFor(el)
      }, OPEN_DELAY_MS)
    },
    onMouseLeave: () => {
      pointerInRef.current = false
      // Held: the bubble carries an outcome the user must still be able to
      // read; only a pending intent open is dropped. It closes when the hold
      // ends (effect above).
      if (hold) { cancelPending(); return }
      hide()
    },
    onFocus: (e: React.FocusEvent) => {
      cancelPending()
      const el = e.currentTarget as HTMLElement
      anchorRef.current = el
      lastAnchorRef.current = el
      keyboardFocusRef.current = !pointerInRef.current
      showFor(el)
    },
    onBlur: () => { keyboardFocusRef.current = false; dismiss() },
  }

  return { tip, tipHandlers, tipId, arm }
}

/** The bubble. Shared chrome here; the caller passes only content and a
 *  variant class for width/wrap (`whitespace-nowrap` for a short two-liner,
 *  `max-w-[26rem] whitespace-pre-wrap break-words` for prose). Pass the
 *  hook's `tipId` so the anchor's `aria-describedby` resolves. */
export function InstantTip({ tip, tipId, className = '', children }: {
  tip: TipPos | null
  tipId?: string
  className?: string
  children: React.ReactNode
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [clampedLeft, setClampedLeft] = useState<number | null>(null)
  // The anchor-left position is measured before the bubble exists, so its
  // width is unknowable at show time. Clamp to the viewport after first
  // paint, on BOTH edges: a right-edge anchor pushes a `position: fixed`
  // bubble past window.innerWidth, and a horizontally scrolled strip
  // (`overflow-x-auto`) can hand us a partially visible anchor whose left is
  // already off-screen — either way clipping exactly the long labels the
  // tooltip exists to recover. The left floor wins when the bubble is wider
  // than the viewport, so the start of the text always survives.
  useLayoutEffect(() => {
    setClampedLeft(null)
    if (!tip) return
    const el = ref.current
    if (!el) return
    const maxLeft = window.innerWidth - 8 - el.offsetWidth
    const next = Math.max(8, Math.min(tip.left, maxLeft))
    if (next !== tip.left) setClampedLeft(next)
  }, [tip])
  if (!tip) return null
  // `above` (the default) anchors the bubble's BOTTOM edge at `top` by pulling
  // it up its own height; `below` anchors its top edge there, so no translate.
  const edge = tip.placement === 'below' ? '' : '-translate-y-full '
  return createPortal(
    <div
      ref={ref}
      id={tipId}
      role="tooltip"
      data-placement={tip.placement ?? 'above'}
      className={`fixed z-[9999] ${edge}rounded-lg border border-border-strong bg-bg-elevated px-2.5 py-1.5 text-[11px] leading-snug shadow-lg pointer-events-none ${className}`}
      style={{ top: tip.top, left: clampedLeft ?? tip.left }}
    >
      {children}
    </div>,
    document.body,
  )
}
