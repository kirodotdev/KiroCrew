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
 * - In running text (`placement: 'flow'`, the message chips) the bubble opens
 *   above from the first line of its flow container (`FLOW_CONTAINER`) and below
 *   from any lower line while it fits inside that container, so it never covers the words that lead up to
 *   the anchor (`flowPlacement`). A held outcome re-anchors the bubble, so the
 *   viewport clamp is re-measured for the new content.
 */
export interface TipPos {
  top: number
  left: number
  /** Which side of the anchor the bubble opens on. Absent means `above`, the
   *  original and default: the bubble's bottom edge sits 8px over the anchor's
   *  top. `below` puts its top edge 8px under the anchor's bottom -- for an
   *  anchor in the top bar, where "above" is off-screen. */
  placement?: TipPlacement
  /** Where a `below` bubble goes instead when it does not fit under the anchor
   *  inside the viewport, or would cross `floor`: the `above` position (first
   *  fragment, boundary lift). Set by `flow` placement only — an explicit
   *  `below` has no above to go to (its anchor is in the top bar) and is
   *  clamped instead. */
  flip?: { top: number; left: number }
  /** The bottom edge a `below` bubble must stay inside: a flow anchor's
   *  container bottom (`flowFloor`) — the message's own box. A bubble that
   *  would cross it lands on what follows the message, so it takes `flip`. */
  floor?: number
}
export type TipPlacement = 'above' | 'below'
/** What a consumer may ask for. `flow` is decided per show (`flowPlacement`):
 *  an inline anchor in running text opens above from the first line of its flow
 *  container, below from any lower line while the bubble fits inside it. */
export type TipPlacementOption = TipPlacement | 'flow'

/** The container a `flow` anchor's "first line" is measured in. `data-tip-flow`
 *  is this hook's own attribute: a consumer sets it on the element that is one
 *  rendered message — the markdown renderer's per-message root, the same element
 *  the lightbox marks `data-image-scope` for its own, unrelated grouping. Two
 *  attributes, one root, no shared meaning: a lightbox change can move its
 *  scope without touching where a bubble opens. The measurement is sound only
 *  while the attribute sits on per-message roots and never on a nested subtree
 *  (a quoted message, an embedded preview) — `closest()` would bind the first
 *  line to that inner scope — pinned by `MarkdownRenderer.inlineCodeCue.test.tsx`
 *  ("data-tip-flow marks the per-message root only, never a nested subtree"). */
const FLOW_CONTAINER = '[data-tip-flow]'

/**
 * Where a `flow` anchor's bubble opens: `above` when the anchor sits on the
 * FIRST line of its flow container (`FLOW_CONTAINER`, the rendered message),
 * `below` from any lower line — where `InstantTip` then keeps it inside the
 * container's box, or flips it above (see `TipPos.floor`).
 *
 * A bubble above a chip on line three covers line two — the words that lead up
 * to the chip, which the reader is in the middle of. Opening it under the chip
 * instead keeps those words readable; what it covers is the line after, which
 * the reader has not reached. On the first line "above" is off the message
 * altogether, so it stays: that is the one place a bubble below would cover
 * prose the reader is heading into.
 *
 * "First line" is read from layout, not guessed from the DOM: the container's
 * first text fragment (a Range over its first text node with a rect) is the
 * first line, and the anchor is on it when its own top starts above that
 * fragment's bottom. Without that measurement — no text in the container, or
 * no layout (jsdom) — the answer is `above`, the position every anchor had
 * before, never a guess.
 */
function flowPlacement(el: HTMLElement, r: DOMRect): TipPlacement {
  const flow = el.closest(FLOW_CONTAINER)
  if (!flow) return 'above'
  const walker = document.createTreeWalker(flow, NodeFilter.SHOW_TEXT)
  const range = document.createRange()
  if (typeof range.getClientRects !== 'function') return 'above'
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!/\S/.test(node.nodeValue ?? '')) continue
    range.selectNodeContents(node)
    const first = range.getClientRects()[0]
    if (!first) continue
    return r.top < first.bottom ? 'above' : 'below'
  }
  return 'above'
}

/** The bottom edge a flow anchor's `below` bubble must not cross: its flow
 *  container's bottom — the message's own box — or, nearer, the bottom of any
 *  ancestor that clips the container (a card capped with `max-h` and
 *  `overflow-hidden`, a scrolling panel). Past either the bubble would sit on
 *  whatever follows the box the reader sees (in a transcript the timestamp and
 *  action row; in a clipped card its meta row or the next item — the bubble is
 *  a portal, so the clip cannot cut it), so a bubble that would cross it takes
 *  its `flip` and opens above, over a line the reader has finished with.
 *  `undefined` when the container has no layout to read. */
function flowFloor(el: HTMLElement): number | undefined {
  const flow = el.closest(FLOW_CONTAINER)
  const rect = flow?.getBoundingClientRect()
  if (!flow || !rect || rect.height === 0) return undefined
  let floor = rect.bottom
  for (let node = flow.parentElement; node && node !== document.body; node = node.parentElement) {
    const overflowY = getComputedStyle(node).overflowY
    if (!overflowY || overflowY === 'visible') continue
    const clip = node.getBoundingClientRect()
    if (clip.height > 0) floor = Math.min(floor, clip.bottom)
  }
  return floor
}

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
 * bubble opens on (`TipPos.placement`), or `flow` to decide per show from the
 * anchor's line in its flow container (`flowPlacement`).
 */
export function useInstantTip({ hold = 0, placement = 'above' }: { hold?: boolean | number; placement?: TipPlacementOption } = {}) {
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
    // The FIRST line fragment, not the bounding box. An inline anchor that
    // wraps — a long inline-code chip — has a bounding box whose top-left
    // corner belongs to no fragment: the top of line one at the left edge of
    // line two, so a bubble placed there floats over unrelated text. For a
    // block anchor, or an inline one on a single line, the two rects are equal.
    const r = el.getClientRects()[0] ?? el.getBoundingClientRect()
    const side: TipPlacement = placement === 'flow' ? flowPlacement(el, r) : placement
    // Lift above the nearest [data-tip-boundary] ancestor, when one exists.
    // In a wrapped chip row the anchor can sit in row 2+, and a bubble opening
    // just above IT covers the row above — the exact chips the user is
    // scanning. Lifting to the boundary's top opens the bubble above the whole
    // strip instead, where only the (transient-safe) message area sits. For a
    // first-row anchor, and for consumers without the attribute (ResizeBadge),
    // this is exactly the old position.
    const boundary = el.closest('[data-tip-boundary]')
    const above = { top: (boundary ? Math.min(r.top, boundary.getBoundingClientRect().top) : r.top) - 8, left: r.left }
    if (side === 'below') {
      // Under the anchor, from the BOUNDING box: the `below` consumer is a
      // single-line top-bar pill, and the fragment rule above exists for a
      // bubble opening above a wrapped inline anchor — below a wrapped one the
      // box's bottom-left IS its last fragment's, where the chip ends. No
      // boundary lift either: the lift exists so a bubble opening ABOVE a
      // wrapped row does not cover the row above it, which a bubble opening
      // below cannot do. A flow anchor carries its above position as the
      // `flip`, and its container's bottom as the `floor`: when below does not
      // fit in the viewport (a chip on the last line of a full-height pane) or
      // would cross the floor (a chip on or near the message's last line — the
      // bubble would sit on the timestamp and action row under the message) the
      // bubble goes above after all; the explicit below consumer has no above
      // to go to and is clamped.
      const b = el.getBoundingClientRect()
      setTip({ top: b.bottom + 8, left: b.left, placement: side, flip: placement === 'flow' ? above : undefined, floor: placement === 'flow' ? flowFloor(el) : undefined })
      return
    }
    setTip(above)
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

  // The hold's edges. A new (truthy) value: (re)open the bubble at the last
  // anchor, which is where the user acted — with the bubble ALREADY open too,
  // because a new outcome is new content: "Copied!" is narrower than the
  // failure notice, and the clamp against the viewport edge ran for the hint
  // that was showing; only a fresh `tip` makes `InstantTip` measure again, so
  // an outcome that skipped this would sit clipped off-screen exactly when it
  // matters. Falling to none: close it, unless the pointer or keyboard focus is
  // still there — then it shows the hint again, and that is a content change
  // too (the wide notice's clamp must not survive into the narrow hint), so
  // it re-shows at the anchor for the same fresh measurement; either way a
  // hint that yielded to this hold gets its turn.
  useEffect(() => {
    if (hold) {
      const el = lastAnchorRef.current
      if (el && el.isConnected) { anchorRef.current = el; showFor(el) }
      return
    }
    if (tip && !pointerInRef.current && !keyboardFocusRef.current) { hide(); return }
    const el = anchorRef.current
    if (tip && el && el.isConnected) showFor(el)
    resumeYieldedHints()
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
  // Where the bubble ended up once its size is known; null while `tip`'s own
  // position stands.
  const [fit, setFit] = useState<{ top: number; left: number; placement: TipPlacement } | null>(null)
  // The anchor position is measured before the bubble exists, so its size is
  // unknowable at show time. Fit it to the viewport after first paint — and
  // again for every new `tip`, which is how a content change (hint -> outcome
  // -> hint) gets its own measurement. Horizontally on BOTH edges: a right-edge
  // anchor pushes a `position: fixed` bubble past window.innerWidth, and a
  // horizontally scrolled strip (`overflow-x-auto`) can hand us a partially
  // visible anchor whose left is already off-screen — either way clipping
  // exactly the long labels the tooltip exists to recover. The left floor wins
  // when the bubble is wider than the viewport, so the start of the text always
  // survives. Vertically for `below`: a chip on the last line of a full-height
  // pane would open the bubble off the bottom; a flow anchor goes above after
  // all (its `flip`), an explicit `below` is clamped to the bottom edge.
  useLayoutEffect(() => {
    setFit(null)
    if (!tip) return
    const el = ref.current
    if (!el) return
    const wanted = tip.placement ?? 'above'
    let { top, left } = tip
    let placement = wanted
    if (wanted === 'below') {
      // The lower of the viewport's bottom edge and the flow floor (the
      // message's own box): a flow bubble that would cross either takes its
      // flip; the explicit top-bar below is clamped to the viewport.
      const limit = Math.min(window.innerHeight - 8, tip.floor ?? Infinity)
      const maxTop = limit - el.offsetHeight
      if (top > maxTop) {
        if (tip.flip) { placement = 'above'; top = tip.flip.top; left = tip.flip.left }
        else top = Math.max(8, maxTop)
      }
    }
    const maxLeft = window.innerWidth - 8 - el.offsetWidth
    left = Math.max(8, Math.min(left, maxLeft))
    if (top !== tip.top || left !== tip.left || placement !== wanted) setFit({ top, left, placement })
  }, [tip])
  if (!tip) return null
  const placement = fit?.placement ?? tip.placement ?? 'above'
  // `above` (the default) anchors the bubble's BOTTOM edge at `top` by pulling
  // it up its own height; `below` anchors its top edge there, so no translate.
  const edge = placement === 'below' ? '' : '-translate-y-full '
  return createPortal(
    <div
      ref={ref}
      id={tipId}
      role="tooltip"
      data-placement={placement}
      className={`fixed z-[9999] ${edge}rounded-lg border border-border-strong bg-bg-elevated px-2.5 py-1.5 text-[11px] leading-snug shadow-lg pointer-events-none ${className}`}
      style={{ top: fit?.top ?? tip.top, left: fit?.left ?? tip.left }}
    >
      {children}
    </div>,
    document.body,
  )
}
