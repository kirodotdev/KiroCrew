import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { i18nT } from '../../i18n/t'

interface PinnedPromptProps {
  /** One-line preview of the pinned prompt. */
  text: string
  /** The full prompt, revealed when expanded. */
  fullText: string
  /** px to translate up so the incoming prompt pushes this banner out of view. */
  pushUp: number
  /** Measured card height, used to shrink the backing band as the card is pushed. */
  bannerH: number
  expanded: boolean
  onToggleExpanded: () => void
  /** Jump the transcript back to this prompt. */
  onJump: () => void
  /** Ref on the card — measured for the push geometry. */
  cardRef: React.Ref<HTMLDivElement>
}

/**
 * Vertical padding a transcript row puts around its bubble (`py-1` on the
 * message row wrapper in ChatPage). The pinned band reproduces it so the card
 * sits the same distance below the fold as a bubble sits below its row top —
 * which is also what makes the hand-off land on the exact same pixel.
 */
const ROW_PAD_Y = 4

/** Expand/collapse height-morph — matches the left-nav collapse
 *  (`grid-template-columns 150ms cubic-bezier(0.2,0,0,1)` in App.tsx). The
 *  chevron rotate below uses the same values so the two move as one. */
const MORPH_MS = 150
const MORPH_EASE = 'cubic-bezier(0.2,0,0,1)'

/**
 * The most recent prompt at or above the fold, pinned under the session title.
 *
 * The card is a pixel-for-pixel copy of the user bubble's own box — same
 * `px-5 mx-auto` content column, right-aligned, `max-w-[550px]`, `px-4 py-1.5
 * rounded-xl bg-card text-sm` with an inner `my-1.5 leading-relaxed` paragraph —
 * because the transcript row it represents is hidden while it is pinned (see
 * ChatPage's row `visibility`). The two are the same size at the same place at
 * the moment of hand-off, so the bubble appears to stop travelling and stick
 * rather than being replaced. Keep these values in sync with `UserMessage`'s
 * `bubble` and with `MD_COMPONENTS.p` in MarkdownRenderer.
 *
 * Deliberate details that protect that equality:
 *   - No `border`. The bubble has none, so a 1px border made the card 2px taller
 *     and shifted its text 1px off the edge — the box visibly changed size as it
 *     pinned. The visible edge is an INSET RING (`ring-1 ring-inset`) instead: it
 *     is painted as a box-shadow, so it reads as a 1px border at zero layout
 *     cost. Do not swap it back to `border-*`.
 *     Pair it with `shadow-sm`, NOT `shadow-md`: the `--shadow-md` token carries
 *     its own outset hairline (`0 0 0 1px`), which is 3% white in dark themes
 *     (invisible) but 4% black in light ones — one pixel outside our inset ring,
 *     so light mode rendered a visible DOUBLE border. `--shadow-sm` has no ring.
 *   - The chevron takes its room from the TEXT, never from the box, and it only
 *     appears once the text is actually truncated. That gate is what keeps the
 *     box honest: a truncated line means the card has already hit its max width,
 *     so inserting the chevron cannot widen it — it only narrows the text
 *     further. A short prompt has no ellipsis, gets no chevron, and keeps hugging
 *     its text exactly like the bubble does. The two states are each stable, so
 *     the measurement below cannot oscillate: "overflowing at width W" still
 *     overflows at W minus the chevron.
 */
export default function PinnedPrompt({
  text, fullText, pushUp, bannerH, expanded, onToggleExpanded, onJump, cardRef,
}: PinnedPromptProps) {
  const textRef = useRef<HTMLParagraphElement | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)
  const lastBoxH = useRef<number | null>(null)
  const [truncated, setTruncated] = useState(false)

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
    const target = el.getBoundingClientRect().height
    const from = lastBoxH.current
    lastBoxH.current = target
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
  }, [expanded])

  useEffect(() => {
    // While expanded the text wraps and stops overflowing, so re-measuring would
    // report "not truncated" and take the chevron away — leaving no way back.
    // Hold the collapsed-state verdict instead; it is re-taken on collapse.
    if (expanded) return
    const el = textRef.current
    if (!el) return
    const measure = () => setTruncated(el.scrollWidth > el.clientWidth + 1)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [text, expanded])

  const showChevron = truncated || expanded

  return (
    <div
      className="relative px-5 py-1 mx-auto w-full pointer-events-none flex justify-end"
      style={{
        maxWidth: 'var(--mc-content-width, 900px)',
        // Clip ONLY while collapsed AND being pushed. The clip is what reveals
        // the card away as the next prompt pushes it up. Two things it must NOT
        // do: (1) clip the EXPANDED card at rest — an expanded prompt grows
        // multi-line past the collapsed band height, and a constant `hidden` cut
        // its lower lines off; (2) reintroduce the transition blink. The blink was
        // never the overflow flip itself — it was the ~4.5px HEIGHT jump that
        // used to accompany it. With the continuous height below, flipping
        // `visible`→`hidden` at pushUp>0 is seamless: the card has 4px of band
        // padding beneath it, enough for `--shadow-sm` (`0 1px 2px`) to still
        // render in the first push frame, so nothing pops.
        overflow: pushUp > 0 && !expanded ? 'hidden' : 'visible',
        // Height must be CONTINUOUS through pushUp === 0, or the clip box jumps
        // the moment the push starts. Carrying both paddings (ROW_PAD_Y * 2)
        // makes this formula equal the natural height at rest and shrink smoothly
        // from there.
        height: bannerH > 0
          ? Math.max(0, ROW_PAD_Y * 2 + bannerH - pushUp)
          : undefined,
      }}
    >
      <div
        ref={cardRef}
        data-testid="pinned-prompt"
        className="pointer-events-auto max-w-[550px] min-w-0"
        style={{ transform: `translateY(${-pushUp}px)`, willChange: 'transform' }}
      >
        <div ref={boxRef} className="flex items-start gap-2 rounded-xl bg-card text-card-fg ring-1 ring-inset ring-border shadow-sm px-4 py-1.5 text-sm">
          <button
            type="button"
            onClick={onJump}
            title={i18nT('pages.chat.pinnedPrompt.jump_to_this_prompt')}
            className="min-w-0 flex-1 bg-transparent border-none p-0 m-0 text-left cursor-pointer"
          >
            <p
              ref={textRef}
              className={`my-1.5 leading-relaxed ${expanded ? 'whitespace-pre-wrap break-words max-h-[40vh] overflow-y-auto' : 'truncate'}`}
              style={expanded ? { overflowWrap: 'anywhere' } : undefined}
            >{expanded ? fullText : text}</p>
          </button>
          {showChevron && (
            <button
              type="button"
              onClick={onToggleExpanded}
              aria-expanded={expanded}
              aria-label={expanded
                ? i18nT('pages.chat.pinnedPrompt.collapse_pinned_prompt')
                : i18nT('pages.chat.pinnedPrompt.expand_pinned_prompt')}
              /* my-1.5 + one line box mirrors the paragraph's own metrics, so the
                 icon centres on the first line and adds no height to the card. */
              className="shrink-0 my-1.5 h-[1.625em] flex items-center bg-transparent border-none p-0 m-0 text-muted hover:text-text transition-colors cursor-pointer"
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
