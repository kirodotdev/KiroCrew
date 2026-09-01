import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react'
import { ChevronDown, ImageOff, Minus } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { ROW_PAD_Y, PINNED_PREVIEW_LINES, pinnedImageUrl } from '../../utils/pinnedPrompt'

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
  /** Measured card height, used to shrink the backing band as the card is pushed. */
  bannerH: number
  expanded: boolean
  onToggleExpanded: () => void
  /** Jump the transcript back to this prompt. */
  onJump: () => void
  /**
   * Collapse the banner to the corner chip (`PinnedPromptPill`). Rendered in the
   * BAND rather than the card's control row — see the note on the band below.
   */
  onMinimize: () => void
  /** Ref on the card — measured for the push geometry. */
  cardRef: React.Ref<HTMLDivElement>
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
 * The most recent prompt that has scrolled fully behind the band, pinned under
 * the session title.
 *
 * The card is a pixel-for-pixel copy of the user bubble's own box — same
 * `px-4 mx-auto` content column, right-aligned, `max-w-[550px]`, `px-4 py-2
 * rounded-xl bg-card text-sm` with an inner `my-1 leading-6` paragraph —
 * because the transcript row it represents is hidden while it is pinned (see
 * ChatPage's row `visibility`). For a one-line prompt the two are the same size
 * at the same place at the moment of hand-off, so the bubble appears to stop
 * travelling and stick rather than being replaced. A taller prompt hands over
 * once its bottom edge reaches the band's bottom (`pinHandoffY`), i.e. once it is
 * completely covered by the band, so the swap still happens out of sight. Keep
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
 *     appears once the text is actually clamped. That gate keeps the box honest: a
 *     clamped line means the card has already hit its max width, so inserting the
 *     chevron cannot widen it — it only narrows the text further. A short prompt is
 *     unclamped, gets no chevron, and keeps hugging its text exactly like the
 *     bubble. The two states are each stable, so the measurement below cannot
 *     oscillate: "overflowing at width W" still overflows at W minus the chevron.
 *
 *     Minimize is NOT in that cluster — it renders in the band, ahead of the card.
 *     The card's row may carry at most two action controls and the jump region plus
 *     the chevron already fill it; siting it outside also leaves the card's box
 *     untouched, so its pixel equality with the bubble survives.
 *   - `PinnedPromptPill` is the minimized form. It is a SEPARATE component rather
 *     than a branch in here on purpose — none of the geometry above applies to a
 *     chip, and reusing this component for it would leave every effect running
 *     against refs that are meaningless in that state.
 *   - Images are shown as thumbnails rather than dropped. `promptPreview` strips
 *     image markdown from the text, so a prompt whose entire content was an
 *     image used to pin as a blank card.
 */
export default function PinnedPrompt({
  text, fullText, images, bodyBeyondPreview, pushUp, bannerH, expanded, onToggleExpanded, onJump, onMinimize, cardRef, onCollapsedHeight,
}: PinnedPromptProps) {
  const textRef = useRef<HTMLParagraphElement | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)
  const lastBoxH = useRef<number | null>(null)
  const [clamped, setClamped] = useState(false)
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

  // Height MORPH on expand/collapse. The card's height is content-driven (the
  // <p> switches truncate↔wrap), so there is no fixed value to CSS-transition
  // between. FLIP it instead: this layout effect runs after React commits the
  // NEW content but before paint, so `getBoundingClientRect` reads the new
  // natural height (`target`); we snap back to the PREVIOUS height (`from`),
  // force a reflow, then transition to `target`. `overflow:hidden` for the
  // duration clips the taller content while the box grows/shrinks so text is
  // revealed/consumed by the moving edge rather than spilling. Keyed on
  // `expanded` only, so scroll-driven pushes (which move the card via transform,
  // not height) never trigger it.
  useLayoutEffect(() => {
    const el = boxRef.current
    if (!el) return
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
    // override in play — i.e. the settled collapsed height whenever this runs
    // collapsed (mount, and every collapse). Reporting it from here is what keeps
    // ChatPage from having to measure the card itself: a measurement taken during
    // the 150ms morph reads an intermediate, up-to-expanded-size height, and the
    // hand-off line derived from it would jump by the difference — hiding a
    // transcript row that is still on screen.
    if (!expanded) onCollapsedHeight?.(target)
    if (from == null || Math.abs(from - target) < 0.5) return
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
  }, [expanded, onCollapsedHeight])

  useEffect(() => {
    // While expanded the text wraps in full and stops overflowing, so re-measuring
    // would report "not clamped" and take the chevron away — leaving no way back.
    // Hold the collapsed-state verdict instead; it is re-taken on collapse.
    if (expanded) return
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
  }, [text, expanded, onCollapsedHeight])

  // Images earn the chevron on their own. Without this an image-only prompt never
  // clamps (no text to clamp), so the readable expanded strip was unreachable and
  // the only recourse was `onJump` — which scrolls away from the position the pin
  // exists to preserve. Widening the box is not a concern in that case: parity with
  // the bubble is already unattainable for a prompt whose bubble is a full-size
  // image, and a clamped prompt has by definition already hit its max width.
  const showChevron = clamped || images.length > 0 || bodyBeyondPreview || expanded

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
        // render in the first push frame, so nothing pops.
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
      {/* Minimize lives in the BAND, not in the card's control row: that row may
          hold at most two action controls and the jump region plus the chevron
          already fill it. It is anchored ADJACENT to the card's left edge rather
          than the band's, because `mr-auto` stranded it at the far left of a ~900px
          column — a bare glyph over the transcript, nowhere near the card whose
          size it controls. Still the card's width away from the chevron, so the two
          cannot be mis-tapped for each other. Opaque (`bg-card` + ring, matching the
          chip) because the band is an overlay the transcript scrolls beneath, so a
          bare glyph sits over passing text — illegible, and readable as content.
          ABSOLUTE, not an in-flow sibling: in flow it cost the card 34px of column
          (w-7 + mr-1.5), so at a clamped width the card came out NARROWER than the
          bubble it stands in for pixel-for-pixel and the hand-off visibly re-wrapped.
          `-mr-4` lands its right edge on the card's own `px-4` padding, so it clears
          the text while needing only 12px of gutter — at 390px the card starts at 16px,
          so 4px is left over, where a full `right-full` offset would have run off-screen. The shared
          wrapper carries the push transform, so control and card travel together. */}
      <div className="relative w-fit max-w-full min-w-0" style={{ transform: `translateY(${-pushUp}px)` }}>
      <button
        type="button"
        data-testid="pinned-prompt-minimize"
        onClick={onMinimize}
        aria-label={i18nT('pages.chat.pinnedPrompt.minimize_pinned_prompt')}
        title={i18nT('pages.chat.pinnedPrompt.minimize_pinned_prompt')}
        className="pointer-events-auto absolute top-0 right-full -mr-4 z-10 shrink-0 flex items-center justify-center h-7 w-7 rounded-full bg-card text-muted ring-1 ring-inset forced-colors:border ring-border shadow-sm border-none p-0 m-0 hover:text-text transition-colors cursor-pointer"
      >
        <Minus size={16} />
      </button>
      <div
        ref={cardRef}
        data-testid="pinned-prompt"
        className="pointer-events-auto max-w-[550px] min-w-0"
        style={{ willChange: 'transform' }}
      >
        <div ref={boxRef} className="flex items-start gap-2 rounded-xl bg-card text-card-fg ring-1 ring-inset forced-colors:border ring-border shadow-sm px-4 py-2 text-sm">
          <button
            type="button"
            onClick={onJump}
            title={i18nT('pages.chat.pinnedPrompt.jump_to_this_turn')}
            className="min-w-0 flex-1 bg-transparent border-none p-0 m-0 text-left cursor-pointer"
          >
            {/* Expanded: images get their own strip at readable size, outside the
                scrollable <p> so they stay put while long text scrolls. */}
            {expanded && shown.length > 0 && (
              <span className="flex flex-wrap gap-2 my-1">
                {shown.map(src => (
                  // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle event (drop the 404'd src so `shown` falls back to the ImageOff glyph), not a user interaction; there is nothing here for a keyboard to reach
                  <img key={src} src={pinnedImageUrl(src)} alt="" loading="lazy"
                    onError={() => markFailed(src)}
                    className="h-20 w-auto max-w-[160px] rounded object-cover ring-1 ring-inset forced-colors:border ring-border" />
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
              className={`my-1 leading-6 ${expanded ? 'whitespace-pre-wrap break-words max-h-[40vh] overflow-y-auto' : 'overflow-hidden'}`}
              style={expanded ? { overflowWrap: 'anywhere' } : {
                // Tailwind ships `line-clamp-<n>` only for a literal n, and the
                // line count is shared with the geometry module — so set the clamp
                // from the constant rather than duplicating it in a class name.
                display: '-webkit-box',
                WebkitBoxOrient: 'vertical',
                WebkitLineClamp: PINNED_PREVIEW_LINES,
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
                  DOWN (see PINNED_PREVIEW_LINES). */}
              {!expanded && shown.map(src => (
                // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- onError is an image-load lifecycle event (drop the 404'd src so `shown` falls back to the ImageOff glyph), not a user interaction; there is nothing here for a keyboard to reach
                <img key={src} src={pinnedImageUrl(src)} alt="" loading="lazy"
                  onError={() => markFailed(src)}
                  className={`inline-block align-middle mr-1.5 rounded-sm object-cover ring-1 ring-inset forced-colors:border ring-border ${
                    text ? 'h-[1.4em] w-[1.4em]' : 'h-[2.8em] w-[3.6em]'}`} />
              ))}
              {/* Every image 404'd (deleted/moved file) AND there is no text: hiding
                  the broken glyphs would put us back at the blank card this change
                  exists to fix, so leave a neutral icon standing in for them. */}
              {!expanded && !text && images.length > 0 && shown.length === 0 && (
                <ImageOff size={20} aria-hidden className="inline-block align-middle text-muted" />
              )}
              {expanded ? fullText : text}
            </p>
          </button>
          {showChevron && (
            /* `my-1` + a one-line box mirrors the paragraph's own metrics, so this
               centres on the first line and adds NO height to the card, which
               `bannerH` and every line derived from it depend on. The button inside
               is a 28px touch target that overflows this 24px box symmetrically via
               `-my-0.5`, so the hit area grows without the card growing. */
            <span className="shrink-0 my-1 h-6 flex items-center">
              <button
                type="button"
                onClick={onToggleExpanded}
                aria-expanded={expanded}
                aria-label={expanded
                  ? i18nT('pages.chat.pinnedPrompt.collapse_pinned_prompt')
                  : i18nT('pages.chat.pinnedPrompt.expand_pinned_prompt')}
                className="h-7 w-7 -my-0.5 flex items-center justify-center bg-transparent border-none p-0 m-0 text-muted hover:text-text transition-colors cursor-pointer"
              >
                <ChevronDown
                  size={16}
                  className={`transition-transform duration-150 ease-[cubic-bezier(0.2,0,0,1)] ${expanded ? 'rotate-180' : ''}`}
                />
              </button>
            </span>
          )}
        </div>
      </div>
      </div>
    </div>
  )
}
