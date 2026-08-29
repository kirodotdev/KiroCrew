import { memo, useRef, useState, useEffect, useCallback } from 'react'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { ChevronLeft, ChevronRight, ArrowUp, X } from 'lucide-react'

import type { OptionAction } from '../app-sdk/protocol/options'
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
   * The local UI action offered alongside the content options (`[OPTION-ACTIONS:]`).
   * Rendered AFTER every content chip, and NOT part of `picked` — an action chip
   * never puts text in the composer, so it has no picked state to carry.
   *
   * Optional so every existing caller keeps typechecking and behaves as before.
   */
  action?: OptionAction | null
  /**
   * The ONLY callback an action chip can reach. Deliberately separate from
   * `onSelect`/`onSend`: an action's click runs a local effect, and routing it
   * through the text-sending callbacks is exactly the leak the plan-action
   * precedent has — a plan chip is interceptable on single click, but its
   * double-click and its `▲` segment still send the label as chat text.
   *
   * Second argument is `sourceKey` as it was at click time, on the same contract
   * as `onSelect`'s third argument: a caller whose dispatch is asynchronous (the
   * close dispatch awaits a breadcrumb write before it tears the tab down)
   * compares it against the current key and refuses a mismatch.
   */
  onAction?: (action: OptionAction, sourceKeyAtClick?: string | null) => void | Promise<unknown>
  /**
   * True when the composer still holds work the user has not sent: typed text,
   * a staged file or directory, a pending session reference.
   *
   * An action chip is BLOCKED while it is true, for the same reason `picked`
   * blocks it — `close` tears the composer down, and the composer's local state
   * is the only copy. `picked` alone was not enough: picks are just one route to
   * a non-empty composer, and typing, pasting or dropping a file reaches none of
   * them, so the chip stayed live over a draft it would discard.
   *
   * Supplied by the host that OWNS the composer state (`ChatInput`); optional so
   * every existing caller keeps typechecking, and absent means "nothing staged",
   * which is the correct reading for the callers that render no action chips.
   */
  composerHasUnsentWork?: boolean
  /** The SLOT-wide answer, so paint and click ask the same question. */
  actionBlockedBySlot?: boolean
  /** An in-flight recording or upload. `composerHasUnsentWork` is already true for
   *  it, but the composer looks EMPTY mid-dictation, so the unsent-draft copy would
   *  name a draft the user can neither see nor clear. Optional for the same reason. */
  composerCaptureInFlight?: boolean
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
function ChipLabel({ option }: { option: string }) {
  return <span className="block truncate">{option}</span>
}

/**
 * Hover text for a chip: the full option, then the gesture hint on its own line.
 *
 * The full label is unconditional. A character-count threshold was the obvious
 * proxy for "is this clamped" and it is the wrong one — truncation depends on the
 * rendered width, the font and the chip's own box, so any fixed number leaves a
 * band of labels visibly cut with no way to read them (at one clamped line the
 * cut starts around 44 characters, so a 60-char threshold missed everything
 * between). `title` takes a `U+000A` per line break, so both fit with no
 * measurement and no component.
 *
 * The DOM keeps the whole string either way, so a screen reader's accessible
 * name is never truncated regardless of this.
 *
 * Joined rather than built as a template literal: with `should-validate-template`
 * the i18n lint reports at the whole template node, so `` `${option}\n\n${hint}` ``
 * counts as an untranslated literal even though both halves are already
 * localized. A bare separator trims to empty and is skipped, which is the
 * accurate outcome — a line break is not copy.
 */
function chipTooltip(option: string, hint: string) {
  return [option, hint].join('\n\n')
}
/** Right-hand "send now" segment class — same palette as the chip body, divided by a border. */
function sendSegmentClassName(isPicked: boolean) {
  // inline-flex + items-center keeps the arrow centred against whatever height
  // the chip body resolves to, so it does not need to know the clamp.
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
  onSelect: (option: string, event: React.MouseEvent, sourceKeyAtClick?: string | null) => void
  onSend?: (text?: string, sourceKeyAtClick?: string | null) => void
  className: string
  /** Position in the row, used for the entrance stagger. */
  index: number
  /** Whether this row is still playing its entrance (see `useChipEntrance`). */
  animating: boolean
  /** Current source-row identity, snapshotted at click time (see FollowUpBarProps). */
  sourceKey?: string | null
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
function Chip({ option, isPicked, picked, quickSend, onSelect, onSend, className, index, animating, sourceKey }: ChipProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // First-click row identity for the in-flight gesture. A double-click is
  // click(detail=1) then dblclick; the footer can be replaced on the reused
  // chip between those two, so onSend must use the key from the FIRST click,
  // not whatever row is current when the second lands.
  const armedSourceKeyRef = useRef<string | null | undefined>(undefined)
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
        // No third argument here on purpose: this path calls onSelect
        // SYNCHRONOUSLY from the click, so there is no window in which the row
        // could advance and nothing for the callee to compare against. Passing
        // `undefined` (i.e. "no key supplied") keeps this path's behaviour
        // exactly as it was — see `sourceKeyAtClick` in the debounced handler,
        // which is where the race actually lives.
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
      onDoubleClick={handleImmediateSend}
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
        onClick={(e) => { e.stopPropagation(); handleImmediateSend() }}
        className={sendSegmentClassName(isPicked)}
      >
        <ArrowUp size={13} />
      </button>
    </span>
  )
}

/**
 * Chrome for an action chip. Same `CHIP_BASE` box and the same colour tokens as a
 * content chip — no new palette — but the accent border is SOLID and present at
 * REST, where a content chip only earns it on hover or once picked. Paired with the
 * leading glyph in `ActionChip`, that is what separates "this does something" from
 * "this sends text".
 *
 * `CHIP_BASE` carries `cursor-pointer`, so the disabled state adds the `disabled:`
 * variant rather than a bare `cursor-not-allowed`: Tailwind emits variants after the
 * plain utility in the same layer, so the variant wins by source order. A bare
 * utility would tie on specificity and resolve on whichever Tailwind happened to
 * emit last.
 */
function actionChipClassName(disabled: boolean, { shrink0 = false }: { shrink0?: boolean } = {}) {
  // DANGER, not accent. The only action is a destructive one, and the accent palette
  // was byte-identical to `chipColors(true)` — a SELECTED content chip — apart from
  // the background tint, so the chip that deletes the session wore the paint that
  // elsewhere means "you already picked this". The same product already paints the
  // same operation `text-danger` in `SessionActionsMenu`, so this is the existing
  // convention rather than a new one. Tokens resolve in every theme
  // (`--danger` / `--danger-subtle` in `index.css`, mapped in `tailwind.config.js`).
  const palette = disabled
    ? 'border-solid border-border text-muted'
    : 'border-solid border-danger/50 text-danger hover:border-danger/70 hover:bg-danger-subtle'
  // Keyed on `aria-disabled`, not `disabled:`. The button never sets the native attribute --
  // a disabled button takes no focus -- so a `disabled:` variant matched nothing.

  // The selector tests the VALUE, since `aria-disabled` is present-and-"false" while merely
  // busy; a bare attribute selector would paint the live chip as blocked.
  const state = 'aria-disabled:opacity-50 aria-disabled:cursor-not-allowed'
  return `${shrink0 ? 'shrink-0 ' : ''}${CHIP_MAX_WIDTH} ${CHIP_BASE} rounded-lg bg-bg-elevated ${state} ${palette}`
}

/**
 * Why an action chip is blocked, or `null` when it is live.
 *
 * ONE value carries both the disabled state and its explanation, deliberately:
 * they were a boolean and a message chosen from it, and that pairing is exactly
 * what let a second block reason arrive wearing the first one's copy. Adding a
 * reason now forces a message for it at the type level.
 */
type ActionBlockReason = 'picks' | 'captureInFlight' | 'unsentWork' | null

/** The reason copy for a blocked chip. ONE source for both the visible helper
 *  text and the hover title, so the two can never say different things. */
function actionBlockReasonText(reason: Exclude<ActionBlockReason, null>): string {
  if (reason === 'picks') return i18nT('components.followUpBar.action_unavailable_while_options_are_selected')
  if (reason === 'captureInFlight') return i18nT('components.followUpBar.action_unavailable_while_capture_in_flight')
  return i18nT('components.followUpBar.action_unavailable_while_the_composer_has_unsent')
}

/**
 * The SHORT visible form of the same reason.
 *
 * The full copy is ~90 chars, and at `text-[11px]` in a narrow column that is 4-6
 * wrapped lines sitting beside single-line chips. `composerHasUnsentWork` flips true
 * on the FIRST typed character, so on any row offering a close chip the bar directly
 * above the caret grew by several lines the moment the user started a reply and
 * collapsed again on send — layout churn on every draft, every time, and worse in
 * the locales whose translation of this string is longer.
 *
 * So the visible text is one line and the CONSEQUENCE is not truncated away: the
 * full sentence is still the `title`, and still rendered in an `sr-only` node that
 * is what `aria-describedby` actually resolves to. A sighted user reads the short
 * form, a screen-reader user hears the whole thing.
 */
function actionBlockReasonShortText(reason: Exclude<ActionBlockReason, null>): string {
  if (reason === 'picks') return i18nT('components.followUpBar.action_unavailable_short_options_selected')
  if (reason === 'captureInFlight') return i18nT('components.followUpBar.action_unavailable_short_capture_in_flight')
  return i18nT('components.followUpBar.action_unavailable_short_unsent_draft')
}

/**
 * Hover text and accessible name for an action chip.
 *
 * Enabled: a FIXED string naming the action, with the label inside it. It used to
 * return the bare `action.label`, and nothing else on screen said what the chip
 * does: the label is model-authored free text, so `[OPTION-ACTIONS: close=That's
 * all]` rendered as `✕ That's all` and tore the session down on one click with no
 * stated consequence — and `confirmCloseSession` defaults to `false`, so there was
 * no dialog either. A user cannot consent to an effect nobody named.
 *
 * The same string is the `aria-label`, so it is the chip's ACCESSIBLE NAME rather
 * than a hover-only extra: a screen-reader user hears the consequence, not just
 * whatever prose the model chose. The visible text stays the label alone, since the
 * glyph plus the danger palette already carry the warning visually.
 *
 * Blocked: the REASON. That case is a convenience only — the reason is ALSO rendered
 * as visible text beside the group and wired via `aria-describedby`, because a
 * disabled button is unfocusable and gets no hover on touch, so a title alone
 * reached neither a keyboard nor a touch user.
 */
function actionChipTitle(action: OptionAction, reason: ActionBlockReason) {
  return reason === null
    ? actionChipAccessibleName(action)
    : actionBlockReasonText(reason)
}

/**
 * The chip's accessible NAME — always the effect plus the label, in every state.
 *
 * Deliberately NOT the block reason when the chip is disabled. A button's name has to
 * identify the button; swapping it for "Unavailable while…" leaves a screen-reader
 * user with a control whose name never says what it would do, and it changes the
 * name out from under anything that addresses the button by it. The reason is already
 * announced as the chip's DESCRIPTION via `aria-describedby`, which is the right
 * relationship for it — name says what it is, description says why it is unavailable.
 */
function actionChipAccessibleName(action: OptionAction) {
  return i18nT('components.followUpBar.action_closes_this_session', { label: action.label })
}

interface ActionChipProps {
  action: OptionAction
  /**
   * Why this chip is blocked, or `null` when it is live. Replaces a bare
   * `disabled` boolean so the state and the explanation come from one value —
   * see `ActionBlockReason`.
   */
  blockReason: ActionBlockReason
  /** Id of the VISIBLE reason node, so the explanation is announced with the
   *  button. A disabled button is unfocusable and gets no hover, so `title`
   *  alone reached neither a keyboard nor a touch user. */
  describedBy?: string
  onAction?: (action: OptionAction, sourceKeyAtClick?: string | null) => void | Promise<unknown>
  className: string
  /** Position in the row, continuing the content chips' stagger ladder. */
  index: number
  animating: boolean
  sourceKey?: string | null
}

/**
 * A chip whose click runs a LOCAL UI action instead of composing or sending text.
 *
 * Deliberately a SEPARATE component from `Chip` rather than a flag threaded through
 * it. `Chip` has THREE routes out to the text path — the undebounced `onSelect`, the
 * debounced `onSelect` plus its `onDoubleClick` → `onSend`, and the `▲` split segment
 * → `onSend` — and the plan-action precedent shows what a partial interception costs:
 * intercepting only the single click leaves a double-click and the `▲` segment sending
 * the label as chat text. A flag would have to be honoured at all three, and a fourth
 * route added later would leak by default. This component closes over neither
 * `onSelect` nor `onSend`, so it CANNOT reach them — the property is structural rather
 * than maintained.
 *
 * Consequently: exactly one `onClick`, no `onDoubleClick`, no debounce timer, and no
 * send segment. There is nothing for a debounce to protect against here (no
 * double-click gesture to cancel), and dispatching synchronously means the
 * `sourceKey` handed to `onAction` is genuinely the one on screen at click time.
 *
 * Blocked while the composer holds anything unsent — a content pick, typed text, or a
 * staged attachment. All three live only in the composer, and an action tears the
 * composer down (the shipped action is `close`), so dispatching would silently discard
 * work the user had assembled. Blocking it is recoverable — clear it and click again —
 * where the discard is not. Picks were the original gate; they turned out to be one
 * route to a non-empty composer rather than the only one.
 */
function ActionChip({ action, blockReason, describedBy, onAction, className, index, animating, sourceKey }: ActionChipProps) {
  const disabled = blockReason !== null
  const entrance = chipEntrance(index, animating)
  // The gate is a REF, not the state below, and that is the whole point: two
  // clicks of a double-click land in the SAME tick, before React re-renders, so a
  // state flag would still read false on the second one and `disabled` would not
  // have been applied yet either. Without this each click ran a full dispatch —
  // two note POSTs, so two durable breadcrumbs for one user action, and two close
  // requests. The state mirror exists only so the chip can PAINT as busy.
  const inFlightRef = useRef(false)
  const [inFlight, setInFlight] = useState(false)
  // A dispatched action tears its own tab down, so the settle can land after this
  // component is gone. Tracked so the release touches no unmounted state.
  const mountedRef = useRef(true)
  useEffect(() => () => { mountedRef.current = false }, [])

  const fire = () => {
    // `aria-disabled` does not stop a click, and that is the point: a natively `disabled`
    // button takes no focus, so its reason was unreachable by keyboard and by touch.
    if (disabled) return
    if (inFlightRef.current) return
    inFlightRef.current = true
    setInFlight(true)
    const release = () => {
      inFlightRef.current = false
      if (mountedRef.current) setInFlight(false)
    }
    // The handler is async in every real host (it awaits a breadcrumb write before
    // closing), so the guard has to hold until it settles. A synchronous handler
    // cannot re-enter on one tick anyway, so releasing immediately is correct there
    // and keeps a non-promise host from latching the chip permanently.
    const result: unknown = onAction?.(action, sourceKey)
    if (result && typeof (result as PromiseLike<unknown>).then === 'function') {
      void Promise.resolve(result).then(release, release)
    } else {
      release()
    }
  }

  return (
    <button
      type="button"
      aria-disabled={disabled || inFlight}
      data-option-action={action.action}
      // Same reason as the content chip: keep keyboard focus in the textarea so a
      // following Enter sends the composer rather than re-activating this chip.
      onMouseDown={(e) => e.preventDefault()}
      // THE one entry point. `sourceKey` is read from the render closure and passed
      // synchronously, so it is the row the user actually clicked on — the callee's
      // own await is where the row can advance, which is what it compares against.
      onClick={fire}
      className={`${className} ${entrance.className}`}
      style={entrance.style}
      title={actionChipTitle(action, blockReason)}
      // Names the effect in EVERY state, so it is the chip's accessible NAME
      // rather than a hover-only extra a screen reader misses. Not the block
      // reason when disabled: that is the DESCRIPTION, already wired below.
      aria-label={actionChipAccessibleName(action)}
      aria-describedby={describedBy}
    >
      <span className="flex items-center gap-1.5 min-w-0">
        {/* Categorical, not decorative: a border weight is a cue a low-contrast or
            colour-blind user can miss, while a glyph is not. `close` is the whole
            enum today, so this is an unconditional X rather than a lookup table
            standing in for a branch that does not exist yet. */}
        <X size={13} className="shrink-0" />
        <ChipLabel option={action.label} />
      </span>
    </button>
  )
}

/**
 * Action chips, in their OWN group rather than as siblings of the content chips.
 *
 * `max-two-buttons-per-row` reads a run of peer buttons as one row, so appending
 * an action chip beside two content options made a three-button row — and the
 * rule's stated reason applies exactly here: peer buttons side by side carry no
 * ranking, and an action chip is not peer to a content option at all. One sends
 * text, the other tears the tab down. The divider is the visible half of that
 * distinction; the separate container is the structural half.
 */
function ActionGroup({
  action, picked, onAction, animating, sourceKey, optionCount, shrink0 = false, composerHasUnsentWork = false,
  actionBlockedBySlot = false,
  composerCaptureInFlight = false,
}: {
  action: OptionAction | null
  picked: ReadonlySet<string>
  onAction?: (action: OptionAction, sourceKeyAtClick?: string | null) => void | Promise<unknown>
  animating: boolean
  sourceKey?: string | null
  optionCount: number
  shrink0?: boolean
  composerHasUnsentWork?: boolean
  /** The SLOT-wide answer, so paint and click ask the same question. */
  actionBlockedBySlot?: boolean
  /** An in-flight recording or upload. `composerHasUnsentWork` is already true for
   *  it, but the composer looks EMPTY mid-dictation, so the unsent-draft copy would
   *  name a draft the user can neither see nor clear. Optional for the same reason. */
  composerCaptureInFlight?: boolean
}) {
  if (!action) return null
  // PICKS FIRST, and the order is load-bearing rather than arbitrary: picking
  // stages the label as composer text, so a picked row ALSO has unsent work and
  // both conditions hold at once. The picks message is the more actionable of the
  // two — it names a specific thing to undo — so it must win, which also keeps
  // the pre-existing copy on the pre-existing case.
  const blockReason: ActionBlockReason =
    picked.size > 0
      ? 'picks'
      : composerCaptureInFlight
        ? 'captureInFlight'
        : (composerHasUnsentWork || actionBlockedBySlot) ? 'unsentWork' : null
  // BOTH scopes, so the paint and the click agree. Painting from this composer alone left a
  // chip in full danger paint that refused on click and wrote a permanent error row.

  // The slot-wide half is resolved by the HOST each render rather than subscribed to: the
  // registry is a plain read, so a value captured once would never un-grey.
  const blocked = blockReason !== null
  // VISIBLE, not title-only. A disabled button takes no focus and receives no
  // hover on touch, so `title` reaches neither a keyboard nor a touch user: they
  // met a greyed `✕` chip with no reason and no route to re-enable it. The same
  // node is wired as the chip's `aria-describedby`, so the reason is announced
  // with the button rather than sitting beside it unlinked.
  const reasonId = blocked ? `action-block-reason-${blockReason}` : undefined
  const reasonText = blocked ? actionBlockReasonText(blockReason) : ''
  const reasonShortText = blocked ? actionBlockReasonShortText(blockReason) : ''
  return (
    <div
      className={`${shrink0 ? 'shrink-0 ' : ''}flex items-end ${CHIP_ROW_GAP} ${optionCount > 0 ? 'ml-1 pl-2 border-l border-border' : ''}`}
    >
      <ActionChip
        action={action}
        blockReason={blockReason}
        describedBy={reasonId}
        onAction={onAction}
        className={actionChipClassName(blocked, { shrink0 })}
        // Continues the ladder past the content chips rather than restarting
        // it, so the row enters as one sequence. No offset to add: there is
        // exactly one action chip, so it is always the next rung.
        index={optionCount}
        animating={animating}
        sourceKey={sourceKey}
      />
      {blocked && (
        // `role="note"` rather than an alert: it explains a control that is
        // already on screen, so it should be readable on demand and not
        // interrupt whatever the user is doing.
        //
        // Two children, not one string. The visible child is the SHORT form and is
        // `aria-hidden` so it is not announced twice; the `sr-only` child carries the
        // full consequence and is what `aria-describedby` resolves to. `line-clamp-2`
        // bounds the visible height even where a translation runs long, so starting a
        // draft can never push the composer down by more than one extra line.

        // Shown whenever the chip is blocked. A hover/focus reveal left this unreachable:
        // the chip was natively `disabled`, so it took no focus for `focus-within` to see.

        // Touch has no hover either, and the reserved width read as a blank gap beside the
        // chip. Always-visible costs one muted line and reaches every input mode.
        <span id={reasonId} role="note" className={`${shrink0 ? 'shrink-0 ' : ''}self-center text-[11px] leading-tight text-muted max-w-[30ch]`}>
          <span aria-hidden="true" className="line-clamp-2">
            {reasonShortText}
          </span>
          <span className="sr-only">{reasonText}</span>
        </span>
      )}
    </div>
  )
}

/** Both layouts render the same chips; `animating` is owned by the parent so a
 *  layout switch cannot restart an entrance that already played. */
type LayoutProps = Omit<FollowUpBarProps, 'layout'> & { animating: boolean }

function ScrollLayout({ options, picked, onSelect, onSend, quickSend, animating, sourceKey, action, onAction, composerHasUnsentWork, actionBlockedBySlot, composerCaptureInFlight }: LayoutProps) {
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

  // Chips changing keeps the row's own box, so no observer reports it. Actions are
  // in the dep list for the same reason options are: they are chips in this row, so
  // an action-only row would otherwise never measure its own overflow.
  useEffect(() => { remeasure() }, [options, action, remeasure])

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
              sourceKey={sourceKey}
            />
          )
        })}
        <ActionGroup
          action={action ?? null}
          picked={picked}
          onAction={onAction}
          animating={animating}
          sourceKey={sourceKey}
          optionCount={options.length}
          composerHasUnsentWork={composerHasUnsentWork}
          actionBlockedBySlot={actionBlockedBySlot}
          composerCaptureInFlight={composerCaptureInFlight}
          shrink0
        />
      </div>
      </div>
    </div>
  )
}

function MultilineLayout({ options, picked, onSelect, onSend, quickSend, animating, sourceKey, action, onAction, composerHasUnsentWork, actionBlockedBySlot, composerCaptureInFlight }: LayoutProps) {
  return (
    // Bottom-aligned for the same reason as the scroll layout: with the
    // one-line clamp every chip is already the same height, so this only
    // decides where a taller chip would sit, and the edge shared with the
    // composer below is the bottom.
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
            sourceKey={sourceKey}
          />
        )
      })}
      <ActionGroup
        action={action ?? null}
        picked={picked}
        onAction={onAction}
        animating={animating}
        sourceKey={sourceKey}
        optionCount={options.length}
        composerHasUnsentWork={composerHasUnsentWork}
          actionBlockedBySlot={actionBlockedBySlot}
        composerCaptureInFlight={composerCaptureInFlight}
      />
    </div>
  )
}

function FollowUpBar({ options, picked, onSelect, onSend, quickSend, layout = 'multiline', sourceKey, action, onAction, composerHasUnsentWork, actionBlockedBySlot, composerCaptureInFlight }: FollowUpBarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // Content-keyed, not identity-keyed: the caller rebuilds the array on every
  // render, so an identity comparison would restart the entrance constantly.
  // \u0000 cannot occur inside an option label.
  //
  // Actions are part of the key because they are part of the row's offer: an
  // action-only row has no options at all, so an options-only key would be the
  // empty string for every such row and the second one in a session would never
  // play its entrance. Byte-identical when there is no action, so no existing
  // row's entrance changes.
  const animating = useChipEntrance(
    [...options, ...(action ? [`${action.action}=${action.label}`] : [])].join('\u0000'),
  )
  if (layout === 'scroll') {
    return <ScrollLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} animating={animating} sourceKey={sourceKey} action={action} onAction={onAction} composerHasUnsentWork={composerHasUnsentWork} actionBlockedBySlot={actionBlockedBySlot} composerCaptureInFlight={composerCaptureInFlight} />
  }
  return <MultilineLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} animating={animating} sourceKey={sourceKey} action={action} onAction={onAction} composerHasUnsentWork={composerHasUnsentWork} actionBlockedBySlot={actionBlockedBySlot} composerCaptureInFlight={composerCaptureInFlight} />
}

export default memo(FollowUpBar)
