import { memo, useRef, useState, useEffect, useCallback } from 'react'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { ChevronLeft, ChevronRight, ArrowUp, Loader2 } from 'lucide-react'
import { InstantTip, useInstantTip as useSharedInstantTip } from './InstantTip'
import ErrorNotice from './ErrorNotice'
import { Glass } from './Glass'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
export type FollowUpLayout = 'multiline' | 'scroll'

interface FollowUpBarProps {
  options: string[]
  picked: ReadonlySet<string>
  /**
   * Third argument is `sourceKey` AS IT WAS AT CLICK TIME (see `sourceKey`
   * below) — `undefined` when the caller does not supply one. Optional so
   * every existing caller keeps typechecking and behaves exactly as before.
   */
  onSelect: (option: string, event: React.MouseEvent, sourceKeyAtClick?: string | null) => void
  /**
   * Immediate send (double-click / Send-now). Second arg is the row identity
   * captured on the FIRST click of the gesture — same snapshot `onSelect`
   * already receives — so a footer that replaces the reused chip between the
   * two clicks of a double-click cannot approve the replacement stage.
   */
  onSend?: (text?: string, sourceKeyAtClick?: string | null) => void
  quickSend?: boolean
  /** 'multiline' (default) wraps onto multiple rows; 'scroll' is a single-line horizontally-scrollable view. */
  layout?: FollowUpLayout
  /**
   * Identity of the transcript row these chips were derived from, when the
   * caller has one (hosts pass their `followUpSourceKey`). Handed BACK to
   * `onSelect` as the click-time snapshot, because a single click is debounced
   * (`FOLLOWUP_CHIP_DEBOUNCE_MS`) and the row can advance inside that window:
   * a byte-identical replacement footer re-renders the same chips WITHOUT
   * remounting them, so the pending timer survives and fires against a row the
   * user never saw. A caller that acts on the click (e.g. the orchestrator
   * plan dispatch) compares the snapshot with its current key and refuses the
   * mismatch — see `usePlanActionMutation`.
   */
  sourceKey?: string | null
  /**
   * Labels whose dispatch is outstanding: each spins and stops taking clicks,
   * every other chip dims. Held while the action is UNACKNOWLEDGED, which
   * outlasts the request — the plan latch survives the HTTP response. A set, not
   * one label: Cancel is never blocked by a pending Go, so both can be
   * outstanding and a single label would un-spin whichever came first. A surface
   * that dispatches nothing (SideChat, ChatEmbed) passes neither prop.
   */
  pendingOptions?: ReadonlySet<string> | null
  /**
   * Labels whose click would be REFUSED — this is what `dim` means. Not "a sibling
   * is busy", which put the disabled look on a live `Cancel` and had a cold reader
   * conclude the stop control was locked. Wider than `pendingOptions` because the
   * single-flight is per class: a held `Go` refuses `Go All`, while `Cancel` keeps
   * its own latch and stays at full strength.
   */
  refusedOptions?: ReadonlySet<string> | null
  /** Detail of the last failed dispatch, `null` when none failed. Any non-null
   *  value draws ONE error row, `''` included — a rejection carrying no readable
   *  message is still a failure to show. */
  error?: string | null
}

/**
 * Option labels are full user-voice instructions and can run to several
 * hundred characters. Left unbounded they size to max-content: in the scroll
 * layout (chips are `shrink-0`) one long option consumed the whole strip and
 * the tail of its text sat outside the visible box, so it read as a single
 * clipped pill with no other option in view. `followup-chip` (index.css) caps
 * the width at half the row minus half the gap — bounded to 18rem..26rem — so
 * two chips fit side by side at ANY composer width, and the label clamps to one
 * line so the truncation is explicit (ellipsis) instead of an
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
 * Duration of the `chip-hop` animation declared in src/tailwind-theme.css. Exported
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
// rather than baked in — see `splitMainChipClassName`. The size follows the
// message font setting (`mc-message-font-chip`, styles/message-font-size.css):
// a chip is conversation text the user reads, not chrome.
// No `cursor-*` here on purpose: the cursor is state-dependent and every chip
// body takes exactly one from `chipStateClass`/`chipCursorClass`. Baking
// `cursor-pointer` in and appending `cursor-default` would decide nothing —
// at equal specificity Tailwind's emission order wins, not attribute order
// (`src/test/narrowFirstBaseline.test.ts`), and `cursor-default` is emitted
// first, so the pointer hand would survive the whole pending state.
// `border-transparent`: the chip keeps its 1px box (the split chip's seam and the
// picked / unpicked swap stay layout-stable) but the glass draws no outline of its
// own — the material has none, the tint and the light bands are the boundary.
const CHIP_BASE = 'px-3 py-1.5 mc-message-font-chip text-left leading-snug transition-all border border-transparent'

// Every chip is its own Liquid Glass pill — the SAME primitive as the composer
// (components/Glass.tsx, `chip` variant), rendered AS the button (`as`), so the
// transcript blurs and bends through the chips the way it does through the box
// beneath them. A picked chip mixes the accent INTO the glass (`glass-accent`)
// instead of swapping to an opaque wash, so it stays the same material; an
// unpicked one brightens a step on hover (`glass-hover`).
function chipColors(isPicked: boolean) {
  return isPicked
    ? 'glass-accent text-accent'
    : 'glass-hover text-muted hover:text-text'
}
/** Every chip pill is rounded 8px (`rounded-lg`); the pane's radius says the same. */
const CHIP_RADIUS = 8

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
  return `flex-1 min-w-0 ${CHIP_BASE} rounded-l-lg border-transparent bg-transparent ${isPicked ? 'text-accent' : 'text-muted hover:text-text'}`
}

/** The split-button wrapper is the glass pill; its two buttons sit on it. */
function splitWrapperClassName(isPicked: boolean) {
  return `rounded-lg transition-all ${chipColors(isPicked)}`
}

/**
 * `truncate` (nowrap + `text-overflow: ellipsis`), not `line-clamp-1`: line
 * clamping ellipsizes after the last whole WORD that fits, which leaves up to
 * a word's width of dead space between the ellipsis and the chip edge when the
 * next word is long. `text-overflow` trims at the character level, so the
 * ellipsis sits flush against the edge on every label. `block` is required:
 * the chip button is not a flex container, and `overflow` cannot clip an
 * inline span.
 *
 * ONE line. A chip is a teaser for the instruction, not the payload —
 * clicking it puts the full text in the composer, and the untruncated string
 * stays in the DOM (accessible name) and on `title` (hover), so the truncation
 * is recoverable. One line keeps every chip the same height by construction
 * rather than by an alignment rule.
 */
function ChipLabel({ option, busy }: { option: string, busy?: boolean }) {
  const label = <span className="block truncate">{option}</span>
  if (!busy) return label
  // Beside the label, not instead of it: the label is WHICH action is running.
  return (
    <span className="flex items-center gap-1.5 min-w-0">
      <Loader2 size={13} aria-hidden="true" className="lucide-inline animate-spin shrink-0" />
      {label}
    </span>
  )
}

/** Dimmed, never `disabled`, and `opacity-70` rather than the 30-50 band this repo
 *  dims DISABLED controls to: a user who clicked Go and needs Cancel must not read
 *  a live chip as locked. Only the busy chip is disabled — a second click on that
 *  one is what the dispatch's own single-flight refuses anyway. */
function chipStateClass(pending: boolean, dimmed: boolean): string {
  if (pending) return ' cursor-default'
  return dimmed ? ' cursor-default opacity-70' : ' cursor-pointer'
}

/** `aria-disabled` follows REFUSAL, not just the spinner: a refused sibling has its
 *  activation dropped by the class latch, so announcing it enabled would leave the
 *  dead button intact for assistive tech while the dim removed it only for sighted
 *  users. It never becomes `disabled` — that would kill the tooltip's dismissal.
 */

/** The pointer half of the above, alone. A split chip's WRAPPER owns the dim, so
 *  the inner button must take only this: applying the full state class to both
 *  compounds the opacity (0.7 x 0.7 = 0.49) straight back into the disabled band
 *  this rule exists to stay out of. */
function chipCursorClass(pending: boolean): string {
  return pending ? ' cursor-default' : ' cursor-pointer'
}

/**
 * Retired by the next dispatch, not by a timer (which races the reading) or a
 * dismiss button. The detail goes in `message` UNPREFIXED, because that is the
 * key `ErrorNotice` looks the structured context up by; a detail-less rejection
 * has nothing to look up, so the sentence becomes the message rather than
 * rendering nothing. `askAgent` ON: a wedged dispatch is the agent's to act on,
 * and the hand-off loses no draft — both hosts park the composer in their
 * slot-draft store.
 */
function ChipError({ error }: { error: string }) {
  const sentence = i18nT('components.followUpBar.plan_action_failed')
  return (
    <ErrorNotice
      variant="inline"
      className="pt-1"
      title={error ? sentence : undefined}
      message={error || sentence}
      askAgent
    />
  )
}

/**
 * Instant hover/focus tooltip carrying the full option text and the gesture
 * hint. This was a native `title` attribute, and the OS hover delay (about a
 * second, not configurable) is what killed it: a clamped label's only readable
 * form sat behind a pause long enough that scanning a row of chips read as
 * "there is no tooltip". The shared `InstantTip` module replaces it and owns
 * the gesture semantics (~100ms hover-intent so a pointer passing through to
 * the composer paints nothing, synchronous show on keyboard focus, Escape and
 * any scroll dismiss, portal past the scroll strip's clipping); this wrapper
 * owns only the chip's content — the full option text, gesture hint below.
 *
 * The full label is unconditional. A character-count threshold was the obvious
 * proxy for "is this clamped" and it is the wrong one — truncation depends on the
 * rendered width, the font and the chip's own box, so any fixed number leaves a
 * band of labels visibly cut with no way to read them (at one clamped line the
 * cut starts around 44 characters, so a 60-char threshold missed everything
 * between).
 *
 * The DOM keeps the whole string either way, so a screen reader's accessible
 * name is never truncated regardless of this.
 */
function useInstantTip(option: string, hint: string) {
  const { tip, tipHandlers, tipId } = useSharedInstantTip()
  const tipNode = (
    <InstantTip tip={tip} tipId={tipId} className="w-max max-w-[min(26rem,calc(100vw-1rem))] whitespace-pre-wrap break-words">
      <div className="text-text text-[12px]">{option}</div>
      <div className="text-muted text-[11px] mt-1">{hint}</div>
    </InstantTip>
  )
  return { tipHandlers, tipNode }
}
/** Right-hand "send now" segment class — same palette as the chip body, divided by a border. */
function sendSegmentClassName(isPicked: boolean, pending: boolean) {
  // inline-flex + items-center keeps the arrow centred against whatever height
  // the chip body resolves to, so it does not need to know the clamp.
  // The cursor follows refusal for the same reason the chip body's does, and by
  // the same exclusive rule rather than by appending: `handleImmediateSend`
  // opens with `if (pending) return`, so a pointer hand here promises a click
  // that is already dropped. The hover accent below is still live while
  // pending — see the disposition on this span.
  return `inline-flex items-center shrink-0 px-1.5 py-1.5 rounded-r-lg ${pending ? 'cursor-default' : 'cursor-pointer'} transition-all border border-transparent border-l-[color:var(--glass-edge)] bg-transparent ${
    isPicked
      ? 'text-accent hover:bg-accent/20'
      : 'text-muted hover:text-accent'
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
  onSelect: (option: string, event: React.MouseEvent, sourceKeyAtClick?: string | null) => void
  onSend?: (text?: string, sourceKeyAtClick?: string | null) => void
  className: string
  /** Position in the row, used for the entrance stagger. */
  index: number
  /** Whether this row is still playing its entrance (see `useChipEntrance`). */
  animating: boolean
  /** Current source-row identity, snapshotted at click time (see FollowUpBarProps). */
  sourceKey?: string | null
  /** THIS chip's dispatch is outstanding (spinner, no clicks) vs another chip's (dim).
   *  Refused in the handlers behind `aria-disabled`, never `disabled`: a disabled
   *  control fires no mouse or focus events, so the tooltip would never get the
   *  `onMouseLeave`/`onBlur` that are its only dismissals, and a focused chip
   *  being disabled drops focus to `<body>`. */
  pending?: boolean
  dimmed?: boolean
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
function Chip({ option, isPicked, picked, quickSend, onSelect, onSend, className, index, animating, sourceKey, pending, dimmed }: ChipProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // First-click row identity for the in-flight gesture. A double-click is
  // click(detail=1) then dblclick; the footer can be replaced on the reused
  // chip between those two, so onSend must use the key from the FIRST click,
  // not whatever row is current when the second lands.
  const armedSourceKeyRef = useRef<string | null | undefined>(undefined)
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const useDebouncedClick = !!onSend && !(quickSend && !isPicked && picked.size === 0)
  // The visible ↑ segment gets its own hint fragment: the blind-read found the
  // click/double-click sentence never names the arrow, so a first-time user
  // "could not tell where the safe click ends and the send click begins". The
  // fragment exists only when the segment does (same condition, see
  // showSendSegment below).
  const hint = chipTitle(isPicked, quickSend, picked, !!onSend)
    + (useDebouncedClick ? ` · ${i18nT('components.followUpBar.tooltip_arrow_sends_now')}` : '')
  const { tipHandlers, tipNode } = useInstantTip(option, hint)
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
  const stateClass = chipStateClass(!!pending, !!dimmed)

  if (!useDebouncedClick) {
    return (
      <>
        {/* The chip IS the glass pane here too (see the standalone chip below). */}
        <Glass
          as="button"
          variant="chip"
          radius={CHIP_RADIUS}
          type="button"
          aria-disabled={pending || dimmed || undefined}
          aria-busy={pending || undefined}
          onMouseDown={(e) => e.preventDefault()}
          // No third argument here on purpose: this path calls onSelect
          // SYNCHRONOUSLY from the click, so there is no window in which the row
          // could advance and nothing for the callee to compare against. Passing
          // `undefined` (i.e. "no key supplied") keeps this path's behaviour
          // exactly as it was — see `sourceKeyAtClick` in the debounced handler,
          // which is where the race actually lives.
          onClick={(e) => { if (pending || dimmed) return; onSelect(option, e) }}
          className={`${className} ${entrance.className}${stateClass}`}
          style={entrance.style}
          {...tipHandlers}
        >
          <ChipLabel option={option} busy={pending} />
        </Glass>
        {tipNode}
      </>
    )
  }

  const handleClick = (e: React.MouseEvent) => {
    // `dimmed` as well as `pending`, and this is the path where it MATTERS: the
    // click below is debounced, so the dispatch happens after the timer, not at
    // the click. A chip dimmed by a sibling's latch could therefore arm a timer,
    // have that latch released inside the window (a definitive 4xx frees it for
    // retry), and dispatch on a chip the user was shown as refused. The hook's
    // `latch.has(vars.slot)` guard cannot catch it — by then the latch is gone.
    if (pending || dimmed) return
    // detail >= 2 means this click is part of a double-click sequence — let
    // onDoubleClick handle it so we don't start a timer that races with it.
    if (e.detail >= 2) return
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    // Capture the parts of the event that survive the timer (React pools events).
    const shiftKey = e.shiftKey
    const synth = { shiftKey, detail: 1 } as unknown as React.MouseEvent
    // Same reason, one level up: the ROW these chips belong to can be replaced
    // inside the debounce window, and a byte-identical replacement footer does
    // not remount this chip — so the timer below outlives the row it was armed
    // on. Snapshot the identity here, at click time, and hand it to onSelect so
    // the callback can tell "the row the user acted on" from "whatever row is
    // current now". Read through the render closure deliberately: a ref would
    // be re-read when the timer fires, which is exactly the bug.
    const sourceKeyAtClick = sourceKey
    armedSourceKeyRef.current = sourceKeyAtClick
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      armedSourceKeyRef.current = undefined
      onSelect(option, synth, sourceKeyAtClick)
    }, FOLLOWUP_CHIP_DEBOUNCE_MS)
  }

  const handleImmediateSend = () => {
    if (pending || dimmed) return
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    const clickedKey = armedSourceKeyRef.current !== undefined ? armedSourceKeyRef.current : sourceKey
    armedSourceKeyRef.current = undefined
    // Pass option text directly to send() so it doesn't race with setInput.
    // If already picked, send() will use the current input (which already contains o).
    onSend?.(isPicked ? undefined : option, clickedKey)
  }

  // Inside the split-button wrapper the WRAPPER (below) is the capped, shrink-0
  // flex item; the button flexes to fill it (see splitMainChipClassName). The
  // plain-button path (no send segment) is the standalone chip, so it keeps the
  // passed-in `className` (cap + rounding + per-layout shrink) unchanged.
  const mainChipClassName = showSendSegment ? `${splitMainChipClassName(isPicked)}${chipCursorClass(!!pending || !!dimmed)}` : `${className} ${entrance.className}${stateClass}`

  const chipProps = {
    type: 'button' as const,
    // Keep keyboard focus in the textarea on click. Without this the chip
    // takes focus, and a follow-up Enter re-activates this (now picked) chip,
    // running the toggle-off branch that deletes the composed input ("the
    // prompt clears"). Deliberate keyboard (tab) activation still toggles.
    'aria-disabled': pending || dimmed || undefined,
    'aria-busy': pending || undefined,
    onMouseDown: (e: React.MouseEvent) => e.preventDefault(),
    onClick: handleClick,
    onDoubleClick: handleImmediateSend,
    className: mainChipClassName,
    ...tipHandlers,
  }
  const label = <ChipLabel option={option} busy={pending} />

  // The standalone chip IS the glass pane (`Glass as="button"`): one element is
  // the flex item, the width cap, the entrance animation and the control, and
  // the pane's layers sit inside it under the label. Inside a split chip the
  // WRAPPER is the pane and this button sits on it transparent, so it is a plain
  // button there.
  const mainChip = showSendSegment ? (
    <button {...chipProps}>{label}</button>
  ) : (
    <Glass as="button" variant="chip" radius={CHIP_RADIUS} {...chipProps} style={entrance.style}>
      {label}
    </Glass>
  )

  if (!showSendSegment) return <>{mainChip}{tipNode}</>

  return (
    // The cap is repeated on the wrapper because the wrapper — not the button —
    // is the flex item here. Without it the wrapper's flex base size is the
    // label's untruncated max-content width (the button's percentage max-width
    // cannot resolve against an indefinite wrapper), leaving a wide empty gap
    // before the next chip. On the flex item the percentage resolves against
    // the strip's definite width.
    // The wrapper is the glass pane here: the two buttons on it stay transparent.
    <Glass as="span" variant="chip" radius={CHIP_RADIUS} className={`inline-flex items-stretch shrink-0 ${CHIP_MAX_WIDTH} ${splitWrapperClassName(isPicked)} ${entrance.className}${stateClass}`} style={entrance.style}>
      {mainChip}
      <button
        type="button"
        aria-label={i18nT('components.followUpBar.send_now_2', { option })}
        title={i18nT('components.followUpBar.send_now')}
        aria-disabled={pending || dimmed || undefined}
        onMouseDown={(e) => e.preventDefault()}
        onClick={(e) => { e.stopPropagation(); handleImmediateSend() }}
        className={sendSegmentClassName(isPicked, !!pending || !!dimmed)}
      >
        <ArrowUp size={13} />
      </button>
      {tipNode}
    </Glass>
  )
}

/** Both layouts render the same chips; `animating` is owned by the parent so a
 *  layout switch cannot restart an entrance that already played. */
type LayoutProps = Omit<FollowUpBarProps, 'layout'> & { animating: boolean }

function ScrollLayout({ options, picked, onSelect, onSend, quickSend, animating, sourceKey, pendingOptions, refusedOptions, error }: LayoutProps) {
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
      {/* The one-line clamp already makes every chip the same height, so this
          only decides where a chip would sit if one ever became taller (an
          icon, a badge, a second line). Bottom, not centre: the strip sits
          directly above the composer, so that is the edge the row is read
          against. */}
      <div ref={setScroller} data-tip-boundary className={`flex ${CHIP_ROW_GAP} overflow-x-auto items-end`} style={{ scrollbarWidth: 'none', msOverflowStyle: 'none' }}>
        {options.map((o, i) => {
          const isPicked = picked.has(o)
          const chipPending = !!pendingOptions?.has(o)
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
              sourceKey={sourceKey}
              pending={chipPending}
              dimmed={!!refusedOptions?.has(o) && !chipPending}
            />
          )
        })}
      </div>
      </div>
      {error != null && <ChipError error={error} />}
    </div>
  )
}

function MultilineLayout({ options, picked, onSelect, onSend, quickSend, animating, sourceKey, pendingOptions, refusedOptions, error }: LayoutProps) {
  return (
    // Bottom-aligned for the same reason as the scroll layout: with the
    // one-line clamp every chip is already the same height, so this only
    // decides where a taller chip would sit, and the edge shared with the
    // composer below is the bottom.
    // data-tip-boundary: the tooltip lifts above this whole wrap, so hovering
    // a chip in row 2+ never hides the row above it (the rows are exactly
    // what the user is scanning; the message area above is transient-safe).
    // A fragment, not a wrapper: with no error the rendered tree is unchanged.
    <>
    <div data-tip-boundary className={`flex ${CHIP_ROW_GAP} flex-wrap pt-1 items-end`}>
      {options.map((o, i) => {
        const isPicked = picked.has(o)
        const chipPending = !!pendingOptions?.has(o)
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
            sourceKey={sourceKey}
            pending={chipPending}
            dimmed={!!refusedOptions?.has(o) && !chipPending}
          />
        )
      })}
    </div>
    {error != null && <ChipError error={error} />}
    </>
  )
}

function FollowUpBar({ options, picked, onSelect, onSend, quickSend, layout = 'multiline', sourceKey, pendingOptions, refusedOptions, error }: FollowUpBarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // Content-keyed, not identity-keyed: the caller rebuilds the array on every
  // render, so an identity comparison would restart the entrance constantly.
  // \u0000 cannot occur inside an option label.
  const animating = useChipEntrance(options.join('\u0000'))
  if (layout === 'scroll') {
    return <ScrollLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} animating={animating} sourceKey={sourceKey} pendingOptions={pendingOptions} refusedOptions={refusedOptions} error={error} />
  }
  return <MultilineLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} animating={animating} sourceKey={sourceKey} pendingOptions={pendingOptions} refusedOptions={refusedOptions} error={error} />
}

export default memo(FollowUpBar)
