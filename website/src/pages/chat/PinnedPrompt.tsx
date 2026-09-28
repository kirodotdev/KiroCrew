import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react'
import { ChevronDown, ImageOff } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { ROW_PAD_Y, PINNED_PREVIEW_LINES, PINNED_RESTING_LINES, pinnedImageUrl } from '../../utils/pinnedPrompt'

interface PinnedPromptProps {
  /** Clamped plain-text preview of the pinned prompt (images stripped out). */
  text: string
  /** The full prompt, revealed when expanded. */
  fullText: string
  /**
   * Image sources the prompt referenced (`promptImages`). Rendered as thumbnails,
   * because `text` has had their markdown removed — without these, a prompt whose
   * whole content was an image pins as an empty card.
   */
  images: string[]
  /**
   * True when `fullText` carries content the preview cannot show at all, so the
   * expand affordance must mount regardless of clamping.
   *
   * A pinned nudge is the case: its preview is a short "cycle N" label that never
   * clamps and it has no images, so neither of the gates below would fire and the
   * instruction body would be unreachable — the same dead end images already have
   * an exemption for.
   */
  bodyBeyondPreview?: boolean
  /** px to translate up so the incoming prompt pushes this banner out of view. */
  pushUp: number
  /**
   * Height the card should be on THIS frame — the progressive fold, computed from
   * the pinned row by `computeLiveCardH`. Absent means "content decides", which is
   * the resting state.
   *
   * While folding this is set as an inline height with NO transition, because it is
   * scroll-driven: the reader's own scrolling is the animation, so a 150ms morph
   * chasing it would lag behind the row it is supposed to track.
   */
  liveH?: number
  /** Measured card height, used to shrink the backing band as the card is pushed. */
  bannerH: number
  expanded: boolean
  onToggleExpanded: () => void
  /** Jump the transcript back to this prompt. */
  onJump: () => void
  /** Ref on the card — measured for the push geometry. */
  cardRef: React.Ref<HTMLDivElement>
  /**
   * Scroll the transcript by `dy` pixels. The card needs this because of where it
   * lives: it sits in a `pointer-events-none` overlay that is a SIBLING of the
   * transcript scroller, never an ancestor. An interactive box there is the target
   * of a wheel, and the browser then looks for a scrollable ANCESTOR of that box —
   * the overlay, then the page — so the transcript never moves and the gesture is
   * swallowed. Measured in a browser: a wheel over the card left the scroller at
   * `scrollTop` 0 while the same wheel over bare scroller moved it 400px.
   *
   * The host owns the scroller, so it does the scrolling; the card only reports the
   * delta. That keeps the card interactive — its text stays selectable and its two
   * buttons stay clickable — which the reader needs while the fold holds the card
   * over content they have not finished reading.
   */
  scrollTranscriptBy?: (dy: number) => void
  /**
   * Reports the card's SETTLED collapsed height. ChatPage derives the hand-off
   * line from it (`pinHandoffY`), so it must never come from measuring the card
   * while the expand/collapse morph below is animating `height` — that samples an
   * expanded-size height and moves the line by the difference.
   */
  onCollapsedHeight?: (h: number) => void
}

/**
 * Vertical padding a transcript row puts around its bubble (`py-1` on the
 * message row wrapper in ChatPage). The pinned band reproduces it so the card
 * sits the same distance below the fold as a bubble sits below its row top —
 * which is also what makes the hand-off land on the exact same pixel. Imported
 * from the geometry module because the hand-off line is derived from the same
 * value (see `pinHandoffY`).
 */
/** Expand/collapse height-morph — matches the left-nav collapse
 *  (`grid-template-columns 150ms cubic-bezier(0.2,0,0,1)` in App.tsx). The
 *  chevron rotate below uses the same values so the two move as one. */
const MORPH_MS = 150
const MORPH_EASE = 'cubic-bezier(0.2,0,0,1)'

/**
 * How long the pointer must rest on the card before the peek opens. Long enough
 * that a pointer crossing the card on its way to the title row does not fire
 * the morph, short enough that a deliberate hover feels immediate. The same
 * order as the morph itself, so open-then-close on a slow transit still reads
 * as one gesture rather than a flicker.
 */
export const PEEK_OPEN_DELAY_MS = 120

/**
 * Frame for the prompt's image thumbnails: a solid `bg-muted` plate showing
 * through the image's own padding. Two things a ring cannot do here:
 *   - `ring-inset` is painted BELOW an `<img>`'s replaced content, so on an
 *     opaque image it never shows at all; a 1px outset ring in the border tone
 *     was measured too faint on the dark card (UX review on #9538: "a black
 *     square on a dark box that I nearly missed", "looks broken"). The plate is
 *     a mid-grey in both themes, so a dark screenshot separates from a dark
 *     card and a white one from a white card regardless of the border tokens.
 *   - Padding renders the background AROUND the content, so the plate is a
 *     real frame, not a shadow that an opaque bitmap can cover.
 * The frame is inside the element's box (sizes below already include it), so
 * the card's pixel parity with the bubble is untouched.
 */
const THUMB_FRAME = 'bg-muted forced-colors:border'

/**
 * The most recent prompt that has scrolled fully behind the band, pinned under
 * the session title.
 *
 * The card is a pixel-for-pixel copy of the user bubble's own box — same
 * `px-4 mx-auto` content column, right-aligned, the bubble's own `max-w-full`
 * cap (so both follow Settings → Chat → Content Width, #8398), `px-4 py-2
 * rounded-xl bg-card text-sm` with an inner `my-1 leading-6` paragraph —
 * because the transcript row it represents is hidden while it is pinned (see
 * ChatPage's row `visibility`; the row's action strip beneath the bubble is
 * re-shown in place by index.css's `[data-pinned-standin]` rule, since this card
 * copies the bubble and nothing below it). For a one-line prompt the two are the
 * same size at the same place at the moment of hand-off, so the bubble appears to
 * stop travelling and stick rather than being replaced. A taller prompt hands
 * over at the same line — its row top on the fold (`pinHandoffY`) — and the card
 * then folds down the bubble's remaining height (`liveH`), so the swap is still a
 * box replaced by an identical box. The
 * box also carries the bubble's `user-bubble` theme hook, so a theme that tints
 * the bubble (kiro-light) tints the card identically and the swap stays
 * invisible there too. Keep
 * these values in sync with `UserMessage`'s `bubble` and with `MD_COMPONENTS.p`
 * in MarkdownRenderer.
 *
 * Deliberate details that protect that equality:
 *   - No `border`. The bubble has none, so a 1px border made the card 2px taller
 *     and shifted its text 1px off the edge — the box visibly changed size as it
 *     pinned. The visible edge is an INSET RING (`ring-1 ring-inset forced-colors:border`) instead: it
 *     is painted as a box-shadow, so it reads as a 1px border at zero layout
 *     cost. Do not swap it back to `border-*`.
 *     Pair it with `shadow-sm`, NOT `shadow-md`: the `--shadow-md` token carries
 *     its own outset hairline (`0 0 0 1px`), which is 3% white in dark themes
 *     (invisible) but 4% black in light ones — one pixel outside our inset ring,
 *     so light mode rendered a visible DOUBLE border. `--shadow-sm` has no ring.
 *   - `items-start` on the band is LOAD-BEARING, not cosmetic. The band's height
 *     is driven by the push (`ROW_PAD_Y * 2 + bannerH - pushUp` below) and the
 *     card is its flex item with no height of its own, so under the default
 *     `align-items: normal` the card is STRETCHED to whatever height the push
 *     leaves — and the card is the element ChatPage measures for `bannerH`. That
 *     closes a loop: pushing shrinks the band, the shrunk band shrinks the
 *     measured card, the smaller `bannerH` shrinks `pinPushTravel`, and the
 *     drop-when-cleared threshold fires against a moving target. Measured with
 *     the real class list: at pushUp 20 the 34.75px card reported 22.75px, and at
 *     full push it reported 0. `items-start` keeps the card at its natural height
 *     so the measurement is a fixed point.
 *   - The chevron takes its room from the TEXT, never from the box, and it only
 *     appears once the text is actually clamped. That gate is what keeps the
 *     box honest: a clamped line means the card has already hit its max width,
 *     so inserting the chevron cannot widen it — it only narrows the text
 *     further. A short prompt is unclamped, gets no chevron, and keeps hugging
 *     its text exactly like the bubble does. The two states are each stable, so
 *     the measurement below cannot oscillate: "overflowing at width W" still
 *     overflows at W minus the chevron.
 *   - Images are shown as thumbnails rather than dropped. `promptPreview` strips
 *     image markdown from the text, so a prompt whose entire content was an
 *     image used to pin as a blank card.
 *
 * Three heights, one box. At REST the text is clamped to `PINNED_RESTING_LINES`
 * (one line) so the card costs the reply beneath it as little as possible. While
 * the pointer is over the card, or a control inside it has keyboard focus, the
 * clamp opens to `PINNED_PREVIEW_LINES` — the PEEK — and closes again on leave.
 * The chevron still EXPANDS to the whole prompt. Every transition between the
 * three runs through the same height morph below, so the card is visibly one
 * object changing size rather than two things swapping (the failure #8714
 * reverted). The peek is pointer-and-keyboard only: a touch `pointerenter` is
 * ignored, because a tap has no matching leave and the card would stick open.
 * The peek and the expansion grow the LIVE card, which the push geometry reads;
 * neither ever re-reports the collapsed height, so the hand-off line stays on
 * the resting height and a hover cannot move it. The peek also closes as soon
 * as the card is being pushed out, so a departing card is always its resting
 * size and the band it slides through is sized for it.
 */
export default function PinnedPrompt({
  text, fullText, images, bodyBeyondPreview, pushUp, liveH, bannerH, expanded, onToggleExpanded, onJump, cardRef, onCollapsedHeight, scrollTranscriptBy,
}: PinnedPromptProps) {
  const textRef = useRef<HTMLParagraphElement | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)
  const lastBoxH = useRef<number | null>(null)
  const [clamped, setClamped] = useState(false)
  const reducedMotion = useReducedMotion()
  // Pointer over the card, or keyboard focus inside it. Two sources feed one
  // flag: a hover peek that closed the moment the pointer left would also close
  // while a keyboard user Tabs onto the chevron — so focus holds it open too.
  // Reset when the pinned prompt changes: the pointer may still be resting where
  // the old card was, and the new card must start at rest like any other.
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  useEffect(() => { setHovered(false); setFocused(false) }, [fullText])
  // Peek only while collapsed AND at rest in the band: expanded already shows
  // everything, and a card being pushed out (`pushUp > 0`) has no reader to
  // peek for — the next prompt is arriving under it. Closing the peek there
  // keeps the push geometry honest: `pinHandoffY` and `pinPushTravel` are
  // derived from the RESTING height, so a three-line card sliding up through a
  // band sized for one line would overhang it. With the peek closed on push,
  // the card is its resting size for the whole departure and the clip below
  // can treat "pushed" and "at rest" as the same shape.
  const peek = !expanded && pushUp <= 0 && (hovered || focused)
  const clampLines = peek ? PINNED_PREVIEW_LINES : PINNED_RESTING_LINES
  // The fold is running: the box is being held at the pinned row's remaining
  // height (see `liveH`). The TEXT has to track that height, not just the box.
  // A grown box with the resting one-line clamp still inside it is a tall empty
  // card sitting on top of the lines the reader has not read yet — the same hole
  // the fold exists to close, moved inside the card. So while folding the
  // paragraph drops its clamp and renders the whole prompt, and the box's own
  // `overflow: hidden` at exactly `liveH` is what trims it: the bottom edge
  // consumes a line at a time as the row leaves, which IS the fold.
  const folding = liveH != null
  // Native listeners on the box rather than JSX handlers: the box is a plain
  // container (its two buttons are the interactive elements), and `pointerenter`
  // / `pointerleave` do not bubble, which is exactly the "over the card as a
  // whole" semantics wanted here. `focusin` / `focusout` DO bubble, so the box
  // hears both buttons.
  useEffect(() => {
    const box = boxRef.current
    if (!box) return
    // Touch has no leave, so a touch `pointerenter` must not open the peek — it
    // would stay open until the next tap. A tap still jumps (the button's
    // onClick) and the chevron is still the way to more than one line.
    //
    // A short intent delay before opening: the card sits top-centre of the
    // reading surface, on the way to the title row and its controls, so a
    // pointer merely crossing it would otherwise fire the grow-then-shrink
    // morph over the reply on every pass. The pointer has to REST on the card
    // for PEEK_OPEN_DELAY_MS; a transit that leaves sooner cancels the timer
    // and nothing moves. Closing is immediate — the pointer has gone.
    let openTimer: ReturnType<typeof setTimeout> | null = null
    const cancelOpen = () => { if (openTimer != null) { clearTimeout(openTimer); openTimer = null } }
    const enter = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return
      cancelOpen()
      openTimer = setTimeout(() => { openTimer = null; setHovered(true) }, PEEK_OPEN_DELAY_MS)
    }
    const leave = () => { cancelOpen(); setHovered(false) }
    // Focus holds the peek only when it arrived by KEYBOARD. A mouse click on the
    // chevron focuses it too, and without this a mouse user who collapsed the
    // card would be left looking at three lines until they clicked elsewhere —
    // the click that said "smaller" would have made it bigger. `pointerdown`
    // runs before the click's default focus action, so a flag set there and
    // consumed by the `focusin` it causes tells the two apart; `pointerup`
    // clears it in case the press moved no focus (button already focused).
    let viaPointer = false
    const down = () => { viaPointer = true }
    const up = () => { viaPointer = false }
    const focusIn = () => { if (!viaPointer) setFocused(true); viaPointer = false }
    // `focusout` fires before the next `focusin` when focus moves between the
    // two buttons, so relatedTarget decides: a move that stays inside the box
    // keeps the peek.
    const focusOut = (e: FocusEvent) => {
      if (e.relatedTarget instanceof Node && box.contains(e.relatedTarget)) return
      setFocused(false)
    }
    box.addEventListener('pointerenter', enter)
    box.addEventListener('pointerleave', leave)
    box.addEventListener('pointerdown', down)
    box.addEventListener('pointerup', up)
    box.addEventListener('focusin', focusIn)
    box.addEventListener('focusout', focusOut)
    return () => {
      cancelOpen()
      box.removeEventListener('pointerenter', enter)
      box.removeEventListener('pointerleave', leave)
      box.removeEventListener('pointerdown', down)
      box.removeEventListener('pointerup', up)
      box.removeEventListener('focusin', focusIn)
      box.removeEventListener('focusout', focusOut)
    }
  }, [])
  // Sources whose fetch failed (a prompt can reference a file that has since been
  // deleted or moved, so `/api/file-raw` 404s). Tracked per-src rather than as one
  // flag so one dead image does not suppress its siblings.
  const [failed, setFailed] = useState<string[]>([])
  const markFailed = useCallback((src: string) => {
    setFailed(prev => (prev.includes(src) ? prev : [...prev, src]))
  }, [])
  // Reset when the pinned prompt changes: `failed` is keyed by src, and a later
  // prompt can legitimately reference a src an earlier one failed on (the file may
  // have been restored), so carrying the verdict forward would hide a live image.
  useEffect(() => { setFailed([]) }, [fullText])
  const shown = images.filter(src => !failed.includes(src))

  // The progressive FOLD owns the box height while it is active. Written here
  // rather than through JSX so there is exactly one writer of `style.height` at a
  // time — the expand / peek morph below is the other, and two writers of one
  // property is how a card ends up stuck at an animation's intermediate value.
  //
  // No transition, on purpose: `liveH` already changes once per scroll frame, so
  // the reader's own scrolling IS the animation. A 150ms ease chasing it would
  // trail the row it is meant to sit flush against, which is the gap this removes.
  useLayoutEffect(() => {
    const el = boxRef.current
    if (!el) return
    if (liveH == null) {
      // Clear only what THIS effect set. A morph in flight owns the property and
      // clears its own value on transitionend.
      if (el.dataset.foldOwned === '1') {
        delete el.dataset.foldOwned
        el.style.height = ''
        el.style.overflow = ''
      }
      return
    }
    el.dataset.foldOwned = '1'
    el.style.transition = ''
    el.style.overflow = 'hidden'
    el.style.height = `${liveH}px`
  }, [liveH])

  // Hand every scroll gesture over the card to the transcript, because the browser
  // will not. The card is interactive (its text is selectable, its buttons work) and
  // it lives in an overlay that is a SIBLING of the scroller, so a wheel here finds
  // no scrollable ancestor and the transcript stays put. Forwarding restores the one
  // thing being interactive costs, and leaves everything it buys.
  //
  // `wheel` and selection do not collide: a drag selects, a wheel scrolls, and they
  // are separate events. `touchmove` is forwarded for the same reason a wheel is —
  // on touch, dragging over the card would otherwise do nothing.
  //
  // Non-passive on purpose: `preventDefault` is what stops the PAGE scrolling
  // instead, and a passive listener may not call it.
  useEffect(() => {
    const box = boxRef.current
    if (!box || !scrollTranscriptBy) return
    // `deltaMode` is not always pixels. Firefox reports lines, and page mode exists
    // too; treating either as pixels would move the transcript by a few px when the
    // reader asked for a screen. The line height to convert with is the PARAGRAPH's
    // (`my-1 leading-6`, 24px), not the box's: `box` is the `.user-bubble` div and its
    // `text-sm` sets line-height to 1.25rem (20px), while `.user-bubble` in index.css
    // only sets `background-color`. Reading the box made a line-mode wheel travel 20px
    // per line instead of 24 — short by a sixth, on every notch.
    const lineHeight = parseFloat(getComputedStyle(textRef.current ?? box).lineHeight) || 24
    const pixels = (e: WheelEvent) => {
      if (e.deltaMode === 1) return e.deltaY * lineHeight
      if (e.deltaMode === 2) return e.deltaY * (box.ownerDocument.defaultView?.innerHeight ?? 800)
      return e.deltaY
    }
    // The card can hold its OWN scroll region: while `expanded` the paragraph is
    // `max-h-[40vh] overflow-y-auto`. Forwarding there is a regression, because the
    // native scroll being cancelled is the reader's way through the prompt they just
    // expanded — and the transcript moving underneath recomputes the pin, so the card
    // can collapse or swap while they are inside it. So the forwarder yields whenever
    // something between the event target and the box can still take the delta, and
    // claims the gesture only once that region is at its edge. While folding the
    // paragraph is `overflow-hidden`, which the overflow check below excludes, so the
    // fold keeps forwarding every gesture.
    const yieldsToInnerScroll = (target: EventTarget | null, dy: number) => {
      let el = target instanceof Element ? target : null
      while (el) {
        if (el.scrollHeight - el.clientHeight > 1) {
          const overflowY = getComputedStyle(el).overflowY
          if (overflowY === 'auto' || overflowY === 'scroll') {
            const room = dy > 0
              ? el.scrollTop + el.clientHeight < el.scrollHeight - 1
              : el.scrollTop > 0
            if (room) return true
          }
        }
        if (el === box) break
        el = el.parentElement
      }
      return false
    }
    const onWheel = (e: WheelEvent) => {
      // Ctrl+wheel is the browser's zoom gesture, and trackpad pinch-zoom arrives as
      // the same event. It is a low-vision path, so it must reach the browser: taking
      // it and scrolling the transcript instead would make the card the one place on
      // the page that cannot be zoomed.
      if (e.ctrlKey) return
      const dy = pixels(e)
      if (!dy) return
      if (yieldsToInnerScroll(e.target, dy)) return
      e.preventDefault()
      scrollTranscriptBy(dy)
    }
    // One finger, tracked across the drag: a touch has no delta of its own, so the
    // distance since the previous move IS the delta, inverted (dragging content up
    // scrolls down).
    let lastY: number | null = null
    const onTouchStart = (e: TouchEvent) => {
      lastY = e.touches.length === 1 ? e.touches[0].clientY : null
    }
    const onTouchMove = (e: TouchEvent) => {
      if (lastY == null || e.touches.length !== 1) return
      const y = e.touches[0].clientY
      const dy = lastY - y
      lastY = y
      if (!dy) return
      // Same yield as the wheel: a drag inside the expanded paragraph is that
      // paragraph's scroll, not the transcript's.
      if (yieldsToInnerScroll(e.target, dy)) return
      e.preventDefault()
      scrollTranscriptBy(dy)
    }
    const onTouchEnd = () => {
      lastY = null
    }
    box.addEventListener('wheel', onWheel, { passive: false })
    box.addEventListener('touchstart', onTouchStart, { passive: true })
    box.addEventListener('touchmove', onTouchMove, { passive: false })
    box.addEventListener('touchend', onTouchEnd, { passive: true })
    box.addEventListener('touchcancel', onTouchEnd, { passive: true })
    return () => {
      box.removeEventListener('wheel', onWheel)
      box.removeEventListener('touchstart', onTouchStart)
      box.removeEventListener('touchmove', onTouchMove)
      box.removeEventListener('touchend', onTouchEnd)
      box.removeEventListener('touchcancel', onTouchEnd)
    }
  }, [scrollTranscriptBy])

  // Height MORPH on expand/collapse and on peek open/close —
  // the fold. The card's height is
  // content-driven (the <p> switches between clamps and full wrap), so there is
  // no fixed value to CSS-transition
  // between. FLIP it instead: this layout effect runs after React commits the
  // NEW content but before paint, so `getBoundingClientRect` reads the new
  // natural height (`target`); we snap back to the PREVIOUS height (`from`),
  // force a reflow, then transition to `target`. `overflow:hidden` for the
  // duration clips the taller content while the box grows/shrinks so text is
  // revealed/consumed by the moving edge rather than spilling. Scroll-driven
  // pushes (which move the card via transform, not height) never trigger it.
  //
  // On a new pin `from` is the BUBBLE's height rather than the outgoing card's:
  // this card is the continuation of that bubble, so the fold starts where the
  // reader was already looking. `text` is in the deps because that is what
  // changes when a different prompt takes the pin.
  useLayoutEffect(() => {
    const el = boxRef.current
    if (!el) return
    // While the fold owns the height, a morph would fight it for the same property
    // and land the card on an intermediate value the row has already scrolled past.
    if (el.dataset.foldOwned === '1') return
    // A toggle landing INSIDE the previous morph leaves that morph's inline
    // height/transition in place — React runs the old effect's cleanup first, and
    // it only detaches the listener. Reading the box now would report the
    // animating value as the natural height (the bug this reporting exists to
    // avoid), so measure where the box visually is, then strip the leftovers so
    // the next read is the true natural height.
    const inflight = !!el.style.height
    const current = inflight ? el.getBoundingClientRect().height : null
    if (inflight) { el.style.height = ''; el.style.transition = ''; el.style.overflow = '' }
    const target = el.getBoundingClientRect().height
    const from = current ?? lastBoxH.current
    lastBoxH.current = target
    // `target` is the natural height React has just committed, read with no inline
    // override in play — i.e. the settled RESTING height whenever this runs at
    // rest (mount, every collapse, every peek close). Reporting it from here is
    // what keeps ChatPage from having to measure the card itself: a measurement
    // taken during the 150ms morph reads an intermediate, up-to-expanded-size
    // height, and the hand-off line derived from it would jump by the difference
    // — hiding a transcript row that is still on screen. A peeked height is never
    // reported for the same reason: it is not where the card rests.
    if (!expanded && !peek) onCollapsedHeight?.(target)
    if (from == null || Math.abs(from - target) < 0.5) return
    // Reduced motion: land on the new height in one step. The peek makes this
    // morph fire on every hover, which is far more often than the chevron did.
    if (reducedMotion) return
    el.style.overflow = 'hidden'
    el.style.height = `${from}px`
    void el.getBoundingClientRect() // force reflow so the next assignment animates
    el.style.transition = `height ${MORPH_MS}ms ${MORPH_EASE}`
    el.style.height = `${target}px`
    const done = (e: TransitionEvent) => {
      if (e.propertyName !== 'height' || e.target !== el) return
      el.style.transition = ''
      el.style.height = ''
      el.style.overflow = ''
      el.removeEventListener('transitionend', done)
    }
    el.addEventListener('transitionend', done)
    return () => el.removeEventListener('transitionend', done)
  }, [expanded, peek, reducedMotion, onCollapsedHeight])

  useEffect(() => {
    // While expanded the text wraps in full and stops overflowing, so re-measuring
    // would report "not clamped" and take the chevron away — leaving no way back.
    // Hold the collapsed-state verdict instead; it is re-taken on collapse. The
    // peek is held out for the same reason, plus one more: its taller box is not
    // the resting height and must not be re-reported as one. Folding is held out
    // for the first reason exactly: the paragraph is unclamped for the duration,
    // so measuring it would read "not clamped" and drop the chevron mid-fold.
    if (expanded || peek || folding) return
    const el = textRef.current
    const box = boxRef.current
    if (!el) return
    const measure = () => {
      // HEIGHT, not width. The collapsed paragraph is a multi-line clamp
      // (`-webkit-line-clamp`), so it never overflows horizontally — every line
      // wraps inside the box and `scrollWidth === clientWidth` always. Only the
      // clamped-away lines show up, as scroll height beyond the visible box.
      setClamped(el.scrollHeight > el.clientHeight + 1)
      // Re-report the collapsed height whenever the box itself resizes. The layout
      // effect above only runs on expand/collapse, so a host font-size or zoom
      // change would otherwise leave ChatPage's hand-off line on a stale height
      // until the next remount. Skipped while an inline height is set — that is
      // the morph animating, and its intermediate values are not the settled
      // height (this also re-reports once `transitionend` clears it).
      if (box && !box.style.height) onCollapsedHeight?.(box.getBoundingClientRect().height)
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    if (box) ro.observe(box)
    return () => ro.disconnect()
  }, [text, expanded, peek, folding, onCollapsedHeight])

  // Images earn the chevron on their own. Without this an image-only prompt never
  // clamps (no text to clamp), so the readable expanded strip was unreachable and
  // the only recourse was `onJump` — which scrolls away from the position the pin
  // exists to preserve. Widening the box is not a concern in that case: parity with
  // the bubble is already unattainable for a prompt whose bubble is a full-size
  // image, and a clamped prompt has by definition already hit its max width.
  // The chevron goes away for the duration of the fold. While folding, the card
  // already renders `fullText` and the fold owns the height, so expanding changes
  // nothing the reader can see: the click would land, `aria-expanded` would flip,
  // and the only visible effect would arrive later, as a snap to the 40vh cap once
  // the fold ends. A control whose feedback is deferred and displaced like that is
  // worse than no control, and the thing it offers is already on screen.
  const showChevron = !folding && (clamped || images.length > 0 || bodyBeyondPreview || expanded)

  return (
    <div
      className="relative px-4 py-1 mx-auto w-full pointer-events-none flex items-start justify-end"
      style={{
        maxWidth: 'var(--mc-content-width, 900px)',
        // Clip ONLY while collapsed AND being pushed. The clip is what reveals
        // the card away as the next prompt pushes it up. Two things it must NOT
        // do: (1) clip the EXPANDED card at rest — an expanded prompt grows
        // multi-line past the collapsed band height, and a constant `hidden` cut
        // its lower lines off; (2) reintroduce the transition blink. The blink is
        // not the overflow flip itself but a ~4.5px HEIGHT jump alongside it.
        // With the continuous height below, flipping
        // `visible`→`hidden` at pushUp>0 is seamless: the card has 4px of band
        // padding beneath it, enough for `--shadow-sm` (`0 1px 2px`) to still
        // render in the first push frame, so nothing pops. The peek needs no
        // term here: it closes the moment `pushUp > 0` (see `peek` above), so a
        // pushed card is always its resting size.
        overflow: pushUp > 0 && !expanded ? 'hidden' : 'visible',
        // Height must be CONTINUOUS through pushUp === 0, or the clip box jumps
        // the moment the push starts. Carrying both paddings (ROW_PAD_Y * 2)
        // makes this formula equal the natural height at rest and shrink smoothly
        // from there. pushUp travels ROW_PAD_Y + bannerH (see computePinPush), so
        // it bottoms out at a ROW_PAD_Y-tall, empty, transparent strip with the
        // card entirely clipped away — no fragment of it survives the no-banner
        // stretch that a tall incoming prompt opens up.
        height: bannerH > 0
          ? Math.max(0, ROW_PAD_Y * 2 + bannerH - pushUp)
          : undefined,
      }}
    >
      <div
        ref={cardRef}
        data-testid="pinned-prompt"
        // Interactive, always. This card sits in a `pointer-events-none` overlay
        // that is a SIBLING of the transcript scroller, so an interactive box here
        // would swallow a wheel: the browser hunts for a scrollable ancestor of the
        // box and finds the overlay, then the page, never the transcript. Going
        // inert dodges that but costs the reader the content — while the fold holds
        // the card over lines they have not read, an inert card cannot be selected,
        // copied or clicked, and its two buttons keep their hover styling while
        // doing nothing. So the gesture is FORWARDED instead (see
        // `scrollTranscriptBy`) and the card keeps its pointer events.
        className="pointer-events-auto max-w-full min-w-0"
        style={{ transform: `translateY(${-pushUp}px)`, willChange: 'transform' }}
      >
        <div
          ref={boxRef}
          className="user-bubble flex items-start gap-2 rounded-xl bg-card text-card-fg ring-1 ring-inset forced-colors:border ring-border shadow-sm px-4 py-2 text-sm"
        >
          <button
            type="button"
            // The jump is suppressed for the duration of the fold. This button wraps the
            // prompt TEXT, and while the fold holds the card at the pinned row's height
            // that text can cover most of the viewport — so a click meant to place a
            // caret or start a selection would instead scroll the transcript away from
            // the place the reader is holding, which is the exact harm this fold exists
            // to prevent. At rest the card is one line and the jump is a deliberate
            // target again.
            onClick={folding ? undefined : onJump}
            title={folding ? undefined : i18nT('pages.chat.pinnedPrompt.jump_to_this_turn')}
            aria-disabled={folding || undefined}
            tabIndex={folding ? -1 : undefined}
            className={`min-w-0 flex-1 bg-transparent border-none p-0 m-0 text-left ${folding ? '' : 'cursor-pointer'}`}
          >
            {/* Expanded: images get their own strip at readable size, outside the
                scrollable <p> so they stay put while long text scrolls. */}
            {expanded && shown.length > 0 && (
              <span className="flex flex-wrap gap-2 my-1">
                {shown.map(src => (
                  // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle event (drop the 404'd src so `shown` falls back to the ImageOff glyph), not a user interaction; there is nothing here for a keyboard to reach
                  <img key={src} src={pinnedImageUrl(src)} alt="" loading="lazy"
                    onError={() => markFailed(src)}
                    className={`h-20 w-auto max-w-[160px] rounded object-cover p-0.5 ${THUMB_FRAME}`} />
                ))}
              </span>
            )}
            {/* The same all-failed fallback the collapsed card gets. Without it,
                expanding an image-only prompt whose files are gone empties the card
                completely — the strip is skipped and `fullText` is '' — so the
                chevron's reward would be a blank box. */}
            {expanded && !fullText && images.length > 0 && shown.length === 0 && (
              <span className="flex my-1">
                <ImageOff size={28} aria-hidden className="text-muted" />
              </span>
            )}
            <p
              ref={textRef}
              // The fold outranks `expanded`. Both can be true at once: the reader can
              // click the chevron while the card is mid-fold. `expanded` caps the text
              // at 40vh, but the fold is holding the BOX at the pinned row's remaining
              // height, so an expanded cap inside a taller box leaves opaque empty card
              // over unread lines — the exact hole this fold exists to close. While
              // folding the text always wraps in full, so the box stays full of text;
              // the cap resumes the moment the fold ends.
              className={`my-1 leading-6 ${folding
                ? 'whitespace-pre-wrap break-words overflow-hidden'
                : expanded
                  ? 'whitespace-pre-wrap break-words max-h-[40vh] overflow-y-auto'
                  : 'overflow-hidden'}`}
              style={expanded || folding ? { overflowWrap: 'anywhere' } : {
                // Tailwind ships `line-clamp-<n>` only for a literal n, and the
                // line counts are shared with the geometry module — so set the
                // clamp from the constants rather than duplicating them in a class
                // name. One line at rest, PINNED_PREVIEW_LINES while peeking.
                display: '-webkit-box',
                WebkitBoxOrient: 'vertical',
                WebkitLineClamp: clampLines,
                overflowWrap: 'anywhere',
              }}
            >
              {/* Collapsed: thumbnails are INLINE LEADING CONTENT of the same
                  paragraph, sized in `em` so they sit in the first line's box and
                  add no height to the card — which is what preserves the card's
                  pixel equality with the bubble it replaces. They also fall inside
                  the line clamp, so a prompt with many images cannot grow the card.
                  Rendering them here (rather than dropping them, as promptPreview
                  does to the text) is what stops an image-only prompt pinning as a
                  blank card.

                  When there is NO text, the em-sized thumbnail is the only content
                  and 1.4em of it is unreadable — so it gets two lines' worth of
                  height instead. Nothing is traded away: parity with the bubble is
                  already unattainable for an image-only prompt, whose bubble is a
                  full-size image, and the taller card only moves the hand-off line
                  DOWN (see PINNED_RESTING_LINES). */}
              {!expanded && shown.map(src => (
                // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle event (drop the 404'd src so `shown` falls back to the ImageOff glyph), not a user interaction; there is nothing here for a keyboard to reach
                <img key={src} src={pinnedImageUrl(src)} alt="" loading="lazy"
                  onError={() => markFailed(src)}
                  className={`inline-block align-middle mr-1.5 rounded-sm object-cover p-px ${THUMB_FRAME} ${
                    text ? 'h-[1.4em] w-[1.4em]' : 'h-[2.8em] w-[3.6em]'}`} />
              ))}
              {/* Every image 404'd (deleted/moved file) AND there is no text: hiding
                  the broken glyphs would put us back at the blank card this change
                  exists to fix, so leave a neutral icon standing in for them. */}
              {!expanded && !text && images.length > 0 && shown.length === 0 && (
                <ImageOff size={20} aria-hidden className="inline-block align-middle text-muted" />
              )}
              {expanded || folding ? fullText : text}
            </p>
          </button>
          {showChevron && (
            <button
              type="button"
              onClick={onToggleExpanded}
              aria-expanded={expanded}
              aria-label={expanded
                ? i18nT('pages.chat.pinnedPrompt.collapse_pinned_prompt')
                : i18nT('pages.chat.pinnedPrompt.expand_pinned_prompt')}
              /* my-1 + one line box mirrors the paragraph's own metrics, so the
                 icon centres on the first line and adds no height to the card. */
              className="shrink-0 my-1 h-6 flex items-center bg-transparent border-none p-0 m-0 text-muted hover:text-text transition-colors cursor-pointer"
            >
              <ChevronDown
                size={16}
                className={`transition-transform duration-150 ease-[cubic-bezier(0.2,0,0,1)] ${expanded ? 'rotate-180' : ''}`}
              />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
