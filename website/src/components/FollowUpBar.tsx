import { memo, useRef, useState, useEffect, useCallback } from 'react'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { ChevronLeft, ChevronRight, ArrowUp } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
export type FollowUpLayout = 'multiline' | 'scroll'

interface FollowUpBarProps {
  options: string[]
  picked: ReadonlySet<string>
  onSelect: (option: string, event: React.MouseEvent) => void
  /** Double-click sends with this option's text directly (bypasses setInput race). */
  onSend?: (text?: string) => void
  quickSend?: boolean
  /** 'multiline' (default) wraps onto multiple rows; 'scroll' is a single-line horizontally-scrollable view. */
  layout?: FollowUpLayout
}

/**
 * Option labels are full user-voice instructions and can run to several
 * hundred characters. Left unbounded they size to max-content: in the scroll
 * layout (chips are `shrink-0`) one long option consumed the whole strip and
 * the tail of its text sat outside the visible box, so it read as a single
 * clipped pill with no other option in view. `followup-chip` (index.css) caps
 * the width at half the row minus half the gap — bounded to 18rem..26rem — so
 * two chips fit side by side at ANY composer width, and the label wraps onto
 * two clamped lines so the truncation is explicit (ellipsis) instead of an
 * invisible overflow. The cap is deliberately relative: the original absolute
 * 26rem was sized against a 900px composer that no default user gets (compact
 * content width is 816px), so it silently forbade the two columns it existed to
 * create — see `CHIP_ROW_GAP` below, which the CSS half-gap is pinned to.
 */
const CHIP_MAX_WIDTH = 'followup-chip'

/**
 * Gap between chips, shared by both layouts. Load-bearing beyond spacing: the
 * width cap in `.followup-chip` subtracts HALF this gap from its 50% preferred
 * width, because two chips plus one gap have to fit the row. Changing this
 * class without changing that CSS breaks the two-column wrap, so
 * `FollowUpBar.test.tsx` pins the two together.
 */
const CHIP_ROW_GAP = 'gap-1.5'

/**
 * Gap between consecutive chips' entrance animations. The whole option set is
 * handed to this component in one render (the tail options are parsed only once
 * the turn ends), so without a ladder every chip would paint in the same frame
 * and the row would blink into existence.
 */
export const FOLLOWUP_CHIP_STAGGER_MS = 55

/**
 * Ceiling on the ladder: chip 7 onwards all share chip 7's delay. A turn can
 * offer more options than the usual three, and an uncapped ladder would leave
 * the last chip of a long row still invisible most of a second after the first
 * one landed — long enough to read as a rendering fault.
 */
export const FOLLOWUP_CHIP_STAGGER_MAX_STEPS = 6

/**
 * Duration of the `chip-hop` animation declared in tailwind.config.js. Exported
 * so a test can pin the two together: the settle window below is built from it,
 * and a CSS duration that outgrew it would end the window mid-hop.
 */
export const FOLLOWUP_CHIP_HOP_DURATION_MS = 420

/**
 * Single-click debounce on a chip that also offers double-click-to-send: the
 * timer this long is what lets a double-click cancel the pending select.
 * Exported so tests advance fake timers against the component's own value
 * instead of a hand-copied literal that silently drifts.
 */
export const FOLLOWUP_CHIP_DEBOUNCE_MS = 220

/**
 * How long the staggered entrance can still be in flight: the deepest rung of
 * the ladder plus one animation.
 */
const CHIP_ENTRANCE_WINDOW_MS = FOLLOWUP_CHIP_STAGGER_MS * FOLLOWUP_CHIP_STAGGER_MAX_STEPS + FOLLOWUP_CHIP_HOP_DURATION_MS

/**
 * True while the current option set is still entering.
 *
 * The entrance is a mount animation, and a chip re-mounts for reasons that have
 * nothing to do with a new option set: picking one chip while Quick Send is on
 * flips every other chip between the plain-button and split-button shapes, and
 * React replaces the element on that shape change. Left ungated the whole row
 * would hop again on every pick. Gating on the option set (not on mount) keeps
 * the entrance to the moment the options actually arrive.
 *
 * Derived during render rather than set from an effect: an effect that switched
 * the entrance on after the first paint would show the chips at rest for one
 * frame and then yank them back to their 0% state.
 */
function useChipEntrance(optionsKey: string): boolean {
  const [settledKey, setSettledKey] = useState<string | null>(null)
  useEffect(() => {
    const timer = setTimeout(() => setSettledKey(optionsKey), CHIP_ENTRANCE_WINDOW_MS)
    return () => clearTimeout(timer)
  }, [optionsKey])
  return settledKey !== optionsKey
}

/** Entrance class + per-chip delay, or nothing once the row has settled. */
function chipEntrance(index: number, animating: boolean): { className: string, style?: React.CSSProperties } {
  if (!animating) return { className: '' }
  const steps = Math.min(index, FOLLOWUP_CHIP_STAGGER_MAX_STEPS)
  return {
    className: 'animate-chip-hop',
    // Omitted for the first chip, which starts immediately — same shape as the
    // Settings/Overview stagger ladder.
    style: steps ? { animationDelay: `${steps * FOLLOWUP_CHIP_STAGGER_MS}ms` } : undefined,
  }
}

// Shape/typography shared by every chip body; the rounding and the flex sizing
// (cap + shrink vs grow) are the only things that differ between a standalone
// chip and the main button of a split-button, so they are supplied per-call
// rather than baked in — see `splitMainChipClassName`.
const CHIP_BASE = 'px-3 py-1.5 text-[13px] text-left leading-snug cursor-pointer transition-all border'

function chipColors(isPicked: boolean) {
  return isPicked
    ? 'border-solid border-accent/50 text-accent bg-accent-subtle'
    : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg-elevated'
}

/** Standalone chip: the flex item itself, so it owns the width cap (and, in the
 *  scroll layout, `shrink-0` so it does not collapse). Fully rounded. */
function chipClassName(isPicked: boolean, { shrink0 = false }: { shrink0?: boolean } = {}) {
  return `${shrink0 ? 'shrink-0 ' : ''}${CHIP_MAX_WIDTH} ${CHIP_BASE} rounded-lg ${chipColors(isPicked)}`
}

/** Main button INSIDE a split-button wrapper. The WRAPPER is the flex item that
 *  carries the cap + `shrink-0`, so this button must flex to fill it and be
 *  allowed to shrink (`flex-1 min-w-0`) — otherwise it claims the wrapper's full
 *  width and the send segment overflows the wrapper box onto the next chip. Only
 *  the left corners round (the send segment rounds the right). Built from the
 *  shared fragments directly, never by string-surgery on `chipClassName`, so a
 *  future utility whose name merely contains `shrink-0`/`followup-chip`/`rounded-lg`
 *  cannot silently rewrite the wrong token and reintroduce the overlap. */
function splitMainChipClassName(isPicked: boolean) {
  return `flex-1 min-w-0 ${CHIP_BASE} rounded-l-lg ${chipColors(isPicked)}`
}

/**
 * The two-line clamp lives on an unpadded inner element on purpose:
 * `-webkit-line-clamp` clips at the padding edge, so clamping the padded
 * button itself leaves a sliver of the third line visible inside its bottom
 * padding.
 */
function ChipLabel({ option }: { option: string }) {
  return <span className="line-clamp-2 break-words">{option}</span>
}

/**
 * Length above which a label is likely clamped. Past it the hover tooltip
 * carries the full option text instead of the click hint — an unreadable label
 * is the more pressing gap, and the gesture hint is still shown on every short
 * chip and on the send segment. The DOM keeps the whole string either way, so
 * the accessible name is never truncated.
 */
const LONG_LABEL_CHARS = 60

function chipTooltip(option: string, hint: string) {
  return option.length > LONG_LABEL_CHARS ? option : hint
}
/** Right-hand "send now" segment class — same palette as the chip body, divided by a border. */
function sendSegmentClassName(isPicked: boolean) {
  // inline-flex + items-center keeps the arrow vertically centred when the
  // chip body wraps onto a second line.
  return `inline-flex items-center shrink-0 px-1.5 py-1.5 rounded-r-lg cursor-pointer transition-all border border-l-0 ${
    isPicked
      ? 'border-solid border-accent/50 text-accent bg-accent-subtle hover:bg-accent/20'
      : 'border-border text-muted hover:text-accent hover:border-accent/40 bg-bg-elevated'
  }`
}

function chipTitle(isPicked: boolean, quickSend: boolean | undefined, picked: ReadonlySet<string>, hasOnSend: boolean) {
  if (isPicked) {
    return hasOnSend
      ? i18nT('components.followUpBar.click_to_remove_from_input_double_click_to_send')
      : i18nT('components.followUpBar.click_to_remove_from_input')
  }
  if (quickSend && picked.size === 0) return i18nT('components.followUpBar.click_to_send_instantly_shift_click_to_select_mu')
  if (quickSend) return i18nT('components.followUpBar.click_to_add_to_selection')
  return hasOnSend
    ? i18nT('components.followUpBar.click_to_add_to_input_double_click_to_select_and')
    : i18nT('components.followUpBar.click_to_add_to_input_editable_before_sending')
}

interface ChipProps {
  option: string
  isPicked: boolean
  picked: ReadonlySet<string>
  quickSend: boolean | undefined
  onSelect: (option: string, event: React.MouseEvent) => void
  onSend?: (text?: string) => void
  className: string
  /** Position in the row, used for the entrance stagger. */
  index: number
  /** Whether this row is still playing its entrance (see `useChipEntrance`). */
  animating: boolean
}

/**
 * Single follow-up chip. Handles click/double-click semantics:
 * - When `onSend` is not provided, falls through to direct `onSelect` (legacy callers).
 * - When `quickSend` is active in instant-send state (not picked, no prior picks), falls through
 *   to direct `onSelect` to preserve the no-lag instant-send UX.
 * - Otherwise: single click is debounced 220ms (timer cancelled by double-click) so the user can
 *   double-click to fire `onSend(text)` directly without going through setInput (which would
 *   race with the React state update and cause send() to read a stale inputRef.current).
 */
function Chip({ option, isPicked, picked, quickSend, onSelect, onSend, className, index, animating }: ChipProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const useDebouncedClick = !!onSend && !(quickSend && !isPicked && picked.size === 0)
  const title = chipTooltip(option, chipTitle(isPicked, quickSend, picked, !!onSend))
  // The entrance belongs on whichever element is this chip's flex item — the
  // button when the chip is standalone, the wrapper when it is a split button.
  // On the inner button of a split chip it would animate the label away from
  // its own send segment.
  const entrance = chipEntrance(index, animating)
  // The visible "send now" segment is the discoverable form of the existing
  // double-click-to-send gesture. Redundant (and hidden) in the quickSend
  // instant-send state, where a single click on an unpicked chip already
  // sends — so it's suppressed there to avoid two controls doing the same
  // thing side by side.
  const showSendSegment = useDebouncedClick

  if (!useDebouncedClick) {
    return (
      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={(e) => onSelect(option, e)}
        className={`${className} ${entrance.className}`}
        style={entrance.style}
        title={title}
      >
        <ChipLabel option={option} />
      </button>
    )
  }

  const handleClick = (e: React.MouseEvent) => {
    // detail >= 2 means this click is part of a double-click sequence — let
    // onDoubleClick handle it so we don't start a timer that races with it.
    if (e.detail >= 2) return
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    // Capture the parts of the event that survive the timer (React pools events).
    const shiftKey = e.shiftKey
    const synth = { shiftKey, detail: 1 } as unknown as React.MouseEvent
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      onSelect(option, synth)
    }, FOLLOWUP_CHIP_DEBOUNCE_MS)
  }

  const handleDoubleClick = () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    // Pass option text directly to send() so it doesn't race with setInput.
    // If already picked, send() will use the current input (which already contains o).
    onSend?.(isPicked ? undefined : option)
  }

  // Inside the split-button wrapper the WRAPPER (below) is the capped, shrink-0
  // flex item; the button flexes to fill it (see splitMainChipClassName). The
  // plain-button path (no send segment) is the standalone chip, so it keeps the
  // passed-in `className` (cap + rounding + per-layout shrink) unchanged.
  const mainChipClassName = showSendSegment ? splitMainChipClassName(isPicked) : `${className} ${entrance.className}`

  const mainChip = (
    <button
      type="button"
      // Keep keyboard focus in the textarea on click. Without this the chip
      // takes focus, and a follow-up Enter re-activates this (now picked) chip,
      // running the toggle-off branch that deletes the composed input ("the
      // prompt clears"). Deliberate keyboard (tab) activation still toggles.
      onMouseDown={(e) => e.preventDefault()}
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      className={mainChipClassName}
      style={showSendSegment ? undefined : entrance.style}
      title={title}
    >
      <ChipLabel option={option} />
    </button>
  )

  if (!showSendSegment) return mainChip

  return (
    // The cap is repeated on the wrapper because the wrapper — not the button —
    // is the flex item here. Without it the wrapper's flex base size is the
    // label's untruncated max-content width (the button's percentage max-width
    // cannot resolve against an indefinite wrapper), leaving a wide empty gap
    // before the next chip. On the flex item the percentage resolves against
    // the strip's definite width.
    <span className={`inline-flex items-stretch shrink-0 ${CHIP_MAX_WIDTH} ${entrance.className}`} style={entrance.style}>
      {mainChip}
      <button
        type="button"
        aria-label={i18nT('components.followUpBar.send_now_2', { option })}
        title={i18nT('components.followUpBar.send_now')}
        onMouseDown={(e) => e.preventDefault()}
        onClick={(e) => { e.stopPropagation(); if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }; onSend?.(isPicked ? undefined : option) }}
        className={sendSegmentClassName(isPicked)}
      >
        <ArrowUp size={13} />
      </button>
    </span>
  )
}

/** Both layouts render the same chips; `animating` is owned by the parent so a
 *  layout switch cannot restart an entrance that already played. */
type LayoutProps = Omit<FollowUpBarProps, 'layout'> & { animating: boolean }

function ScrollLayout({ options, picked, onSelect, onSend, quickSend, animating }: LayoutProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const [attachEdges, edges, remeasure] = useScrollEdges<HTMLDivElement>()

  // The hook owns the node's edge measurement; this keeps a plain handle to the
  // same node for the row's own scroll and wheel behaviour.
  const setScroller = useCallback((node: HTMLDivElement | null) => {
    scrollRef.current = node
    attachEdges(node)
  }, [attachEdges])

  // A mount effect is enough here: this scroller renders unconditionally with
  // ScrollLayout, so its node exists by the time effects run — unlike the tab
  // strip, which appears only below a breakpoint and is why the hook binds from
  // a ref callback.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    // Vertical wheel scrolls the row horizontally, but only while the row
    // actually overflows — otherwise the page loses its own scroll.
    const onWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return
      if (el.scrollWidth <= el.clientWidth) return
      e.preventDefault()
      el.scrollLeft += e.deltaY
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  // Chips changing keeps the row's own box, so no observer reports it.
  useEffect(() => { remeasure() }, [options, remeasure])

  // Scroll by ~80% of the visible width in the given direction, so a click
  // reveals the next set of chips while keeping one in view for continuity.
  const scrollByDir = useCallback((dir: -1 | 1) => {
    const el = scrollRef.current
    if (!el) return
    el.scrollBy({ left: dir * Math.max(el.clientWidth * 0.8, 120), behavior: 'smooth' })
  }, [])

  // Small solid, vertically-centered pill button so the arrow reads as a
  // distinct control instead of a transparent icon colliding with the chip
  // text underneath it. The opaque background masks the faded edge chip.
  const arrowClass = 'absolute top-1/2 -translate-y-1/2 z-20 flex items-center justify-center h-6 w-6 rounded-full bg-bg-elevated border border-border text-muted hover:text-text hover:border-accent/40 shadow-sm cursor-pointer p-0'

  return (
    <div className="pt-1">
      <div className="relative">
      {edges.left && <div className="absolute left-0 top-0 bottom-0 w-10 z-10 pointer-events-none bg-gradient-to-r from-bg to-transparent" />}
      {edges.right && <div className="absolute right-0 top-0 bottom-0 w-10 z-10 pointer-events-none bg-gradient-to-l from-bg to-transparent" />}
      {edges.left && (
        <button
          type="button"
          aria-label={i18nT('components.followUpBar.scroll_suggestions_left')}
          title={i18nT('components.followUpBar.scroll_left')}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => scrollByDir(-1)}
          className={`${arrowClass} left-0.5`}
        >
          <ChevronLeft size={16} />
        </button>
      )}
      {edges.right && (
        <button
          type="button"
          aria-label={i18nT('components.followUpBar.scroll_suggestions_right')}
          title={i18nT('components.followUpBar.scroll_right')}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => scrollByDir(1)}
          className={`${arrowClass} right-0.5`}
        >
          <ChevronRight size={16} />
        </button>
      )}
      {/* Chips are bottom-aligned, not centred: a long label clamps onto two
          lines and makes its chip taller than the rest, and centring floats
          every single-line chip into the middle of that taller row. The shared
          edge is the BOTTOM one because the strip sits directly above the
          composer, so that is the edge the row is read against. */}
      <div ref={setScroller} className={`flex ${CHIP_ROW_GAP} overflow-x-auto items-end`} style={{ scrollbarWidth: 'none', msOverflowStyle: 'none' }}>
        {options.map((o, i) => {
          const isPicked = picked.has(o)
          return (
            <Chip
              key={o}
              option={o}
              isPicked={isPicked}
              picked={picked}
              quickSend={quickSend}
              onSelect={onSelect}
              onSend={onSend}
              className={chipClassName(isPicked, { shrink0: true })}
              index={i}
              animating={animating}
            />
          )
        })}
      </div>
      </div>
    </div>
  )
}

function MultilineLayout({ options, picked, onSelect, onSend, quickSend, animating }: LayoutProps) {
  return (
    // Bottom-aligned for the same reason as the scroll layout: a two-line
    // clamped chip must not float its single-line neighbours onto its own
    // centre line, and the edge shared with the composer below is the bottom.
    <div className={`flex ${CHIP_ROW_GAP} flex-wrap pt-1 items-end`}>
      {options.map((o, i) => {
        const isPicked = picked.has(o)
        return (
          <Chip
            key={o}
            option={o}
            isPicked={isPicked}
            picked={picked}
            quickSend={quickSend}
            onSelect={onSelect}
            onSend={onSend}
            className={chipClassName(isPicked)}
            index={i}
            animating={animating}
          />
        )
      })}
    </div>
  )
}

function FollowUpBar({ options, picked, onSelect, onSend, quickSend, layout = 'multiline' }: FollowUpBarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // Content-keyed, not identity-keyed: the caller rebuilds the array on every
  // render, so an identity comparison would restart the entrance constantly.
  // \u0000 cannot occur inside an option label.
  const animating = useChipEntrance(options.join('\u0000'))
  if (layout === 'scroll') {
    return <ScrollLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} animating={animating} />
  }
  return <MultilineLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} animating={animating} />
}

export default memo(FollowUpBar)
