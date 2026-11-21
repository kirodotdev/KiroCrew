import type { AriaAttributes, ComponentType, ReactNode } from 'react'
import { useSyncExternalStore } from 'react'
import { AlertCircle, AlertTriangle, Info, Sparkles, X } from 'lucide-react'
import AskAgentButton, { handoffErrorToAgent } from './AskAgentButton'
import { isGatewayConnected, subscribeGatewayConnected, type ErrorReport } from '../utils/errorReport'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { withOriginLink } from './withOriginLink'

export type ErrorNoticeMenuItemComponent = ComponentType<{
  offline?: boolean
  title?: string
  disabled?: boolean
  'aria-describedby'?: string
  'aria-disabled'?: AriaAttributes['aria-disabled']
  'aria-label'?: string
  onSelect?: (event: Event) => void
  children?: ReactNode
}>

/**
 * The agent hand-off as a real Radix menu item.
 *
 * A menu's roving focus reaches items, not a button nested inside another item.
 * Hosts opt in by rendering this as a sibling after the item that owns the
 * notice. `describedBy` points back to that passive alert. Successful selection
 * follows Radix's normal close path. A staging failure prevents that close so
 * the diagnostic and recovery action stay visible.
 *
 * `outcome` is an optional one-line consequence rendered under the label, for a
 * host whose every other item names its outcome in a sub-line: there, a bare
 * "Ask the agent" was the one control a reader could not identify ("no idea
 * what it does or why it's in this menu"). The host words it for the notice it
 * follows; the tooltip keeps describing the mechanism.
 *
 * `onHandoff` runs only after the hand-off staged. A host that sits inside a
 * surface which closes on navigation (the nav drawer) passes its close here,
 * because the hand-off may stay on `/chat` and so change no pathname.
 */
export function ErrorNoticeMenuItem({
  Item,
  message,
  describedBy,
  outcome,
  report,
  onHandoff,
}: {
  Item: ErrorNoticeMenuItemComponent
  message?: string | null
  describedBy: string
  outcome?: string
  /** The structured report, for a host whose `message` is its own rewording
   *  and so differs from the journal entry `findReport` looks up. */
  report?: ErrorReport
  onHandoff?: () => void
}) {
  // Same gate as AskAgentButton, for the same reason: while the gateway is
  // unreachable the hand-off delivers the error to a composer that cannot send.
  const gatewayConnected = useSyncExternalStore(subscribeGatewayConnected, isGatewayConnected)
  if (!message || !gatewayConnected) return null

  return (
    <Item
      title={i18nT('components.askAgent.open_a_chat_with_this_error_s_context_attached')}
      aria-describedby={describedBy}
      onSelect={(event) => {
        if (!handoffErrorToAgent({ message, report })) {
          event.preventDefault()
          return
        }
        onHandoff?.()
      }}
    >
      <Sparkles size={13} className="shrink-0 text-muted" aria-hidden="true" />
      {outcome ? (
        <span className="flex min-w-0 flex-col">
          <span className="truncate">{i18nT('components.askAgent.ask_the_agent')}</span>
          <span className="truncate text-[11px] text-muted">{outcome}</span>
        </span>
      ) : (
        i18nT('components.askAgent.ask_the_agent')
      )}
    </Item>
  )
}

/**
 * The fourth hand-off state: no hand-off, because the agent it would open
 * cannot run. The agent's own credential or backend is what failed (on the KAS
 * backend every agent runs as the stored Kiro sign-in, so an expired sign-in
 * notice that offered "Ask the agent" would open a chat that fails the same
 * way).
 *
 * `noHandoffReason` is a sentence for the reviewer, never rendered. Setting it
 * together with `askAgent`, `askAgentLabel` or `onHandoff` does not typecheck:
 * a notice either hands off or states why it cannot. It is NOT the form for a
 * notice next to an unsaved draft; that one keeps `askAgent` off with a
 * `No hand-off:` comment naming the draft (see `errors-use-error-notice` in
 * `website/AUTOSDE.yaml`).
 */
export type ErrorNoticeNoHandoff =
  | { noHandoffReason?: undefined }
  | {
    noHandoffReason: string
    askAgent?: never
    askAgentLabel?: never
    onHandoff?: never
    onStage?: never
    gate?: never
  }

export type ErrorNoticeImpact = 'blocking' | 'partial' | 'minor'

const LEVELS = {
  blocking: {
    Icon: AlertCircle,
    ink: 'text-danger-ink', inkNested: 'text-danger-ink-nested',
    body: 'text-danger-body', bodyNested: 'text-danger-body-nested',
    title: 'font-semibold',
    box: 'border border-danger/40 bg-danger-subtle', rule: 'border-danger/25',
  },
  partial: {
    Icon: AlertTriangle,
    ink: 'text-warn-text', inkNested: 'text-warn-text',
    body: 'text-text-strong', bodyNested: 'text-text-strong',
    title: 'font-semibold',
    box: 'border border-warn/40 bg-warn-subtle', rule: 'border-warn/25',
  },
  minor: {
    Icon: Info,
    ink: 'text-muted', inkNested: 'text-muted',
    // On a host's warn tint the muted grey falls below 4.5:1 in some light
    // palettes, so a nested minor message reads in the strong text colour.
    body: 'text-muted', bodyNested: 'text-text-strong',
    title: 'font-medium text-text-strong',
    box: '', rule: 'border-border',
  },
} as const

/**
 * The host's own buttons in the action row look like real buttons at every
 * level: a card fill, a border that clears 3:1 on every notice tint, padding,
 * a hover state and the focus ring. Scoped to direct `<button>` children, so
 * "Ask the agent" (wrapped in a span) stays a link.
 */
const ACTION_BUTTONS = [
  '[&>button]:inline-flex [&>button]:items-center [&>button]:gap-1 [&>button]:cursor-pointer',
  '[&>button]:bg-card [&>button]:text-text-strong [&>button]:border [&>button]:border-solid [&>button]:border-notice-btn-border',
  '[&>button]:rounded-md [&>button]:px-3 [&>button]:py-[5px] [&>button]:text-[12.5px] [&>button]:font-medium [&>button]:leading-[1.2] [&>button]:shadow-sm',
  '[&>button:hover]:bg-bg-hover [&>button:hover]:border-notice-btn-border-hover',
  '[&>button:focus-visible]:outline-2 [&>button:focus-visible]:outline-solid [&>button:focus-visible]:outline-offset-2 [&>button:focus-visible]:outline-notice-ring',
  '[&>button:disabled]:opacity-50 [&>button:disabled]:cursor-not-allowed',
].join(' ')

/**
 * The shared error surface — one place that renders an error *and* offers to
 * hand it to the agent.
 *
 * Replaces the ad-hoc `{err && <div className="text-danger">{err}</div>}` shape
 * repeated across the dashboard. The migration is deliberately trivial: pass the
 * same string the call site already had and the structured context (endpoint,
 * status, backend `code`) is recovered from the error journal
 * (`utils/errorReport`), so no call site has to start threading an error object.
 *
 * Pass `report` instead when the caller genuinely holds one (a caught
 * `ApiError`), which skips the message-match lookup.
 *
 * ## Two variants, because the dashboard has two shapes
 *
 * `block` is the boxed banner at the top of a panel. `inline` is the compact
 * run of text that sits inside an existing button row — those sites are laid out
 * as flex children, so dropping a bordered box into one would break the row. The
 * variant is a layout choice only; both carry the same agent hand-off.
 *
 * ## How loud: `impact`
 *
 * The look follows what the failure costs on this surface
 * (docs/request-for-change/rfc-error-notice-impact.md). `blocking` (the
 * default) when what the user came to do failed: the main read, a pressed
 * action, a gate. Red, `role="alert"`. `partial` when the surface works and one
 * part of it is missing or stale: amber, `role="status"`. `minor` when only a
 * detail is missing and nothing the user does depends on it: a grey line,
 * `role="status"`. The default is the loud one, so a missing prop never hides
 * a real failure. `nested` is for a notice inside a host that already frames
 * it (a warning box): it draws no box of its own.
 *
 * The block variant has one layout: icon and title on one line, the message
 * under the title, then one action row with the host's own buttons
 * (`actions`) and "Ask the agent".
 */
export default function ErrorNotice({
  id,
  message,
  report,
  title,
  onDismiss,
  dismissLabel,
  dismissLabelVisible = false,
  variant = 'block',
  impact = 'blocking',
  nested = false,
  actions,
  askAgent = false,
  askAgentLabel,
  actionPlacement = 'beside',
  messagePlacement = 'beside',
  scrollMessage = false,
  footer,
  onHandoff,
  onStage,
  gate,
  className = '',
  messageClassName = '',
  messageTooltip,
  messageRef,
  testId,
  showIcon = true,
  handoffHint,
  announce = true,
}: {
  /** DOM id for controls, including menu hand-offs, that describe themselves with this alert. */
  id?: string
  /** Human error text. Falsy renders nothing, so `<ErrorNotice message={err} />` needs no `&&` guard. */
  message?: string | null
  /** Structured report, when known. Otherwise looked up by `message`. */
  report?: ErrorReport
  /**
   * Optional bold lead ("Save failed"). Kept separate from `message` rather than
   * concatenated, because `message` is the journal lookup key — prefixing it
   * would lose the structured context this component exists to recover.
   */
  title?: string
  /** Renders a dismiss affordance when provided. */
  onDismiss?: () => void
  /**
   * Name of the dismiss control — its accessible name AND its tooltip — when
   * the bare "Dismiss" would leave out what the click commits to: a notice
   * whose dismissal is REMEMBERED (it stays away on the next visit until its
   * condition changes) owes the user that promise where they can read it
   * before clicking, sighted or not. Ignored without `onDismiss`. The control
   * only ever removes the notice. How loud the notice is comes from `impact`,
   * which is chosen by what the failure costs, never by a wish to hide it: a
   * caller that wants the notice gone hides it.
   */
  dismissLabel?: string
  /**
   * Show `dismissLabel` as VISIBLE text beside the ✕ -- the same one string,
   * read three ways (text, accessible name, tooltip) -- when dismissing is a
   * step in the user's task rather than the closing of a notice ("Dismiss
   * notice and re-enable Install" on a banner whose dismissal re-enables the
   * Install button), so the path back is discoverable without reading the
   * paragraph that mentions it. Off by default: every other consumer keeps the
   * icon-only ✕ at the top right. Ignored without `onDismiss`. In the block
   * variant a control with text is wide, so it ends the action row instead.
   */
  dismissLabelVisible?: boolean
  /** `block` = boxed banner; `inline` = compact text for an existing flex row. */
  variant?: 'block' | 'inline'
  /**
   * What the failure costs on this surface; picks the colour, icon, frame and
   * role. `blocking` (default): what the user came to do failed, a pressed
   * action failed, or a gate cannot pass. `partial`: the surface works, one
   * part of it is missing or stale. `minor`: only a detail is missing and
   * nothing the user does depends on it. A pressed action's failure is always
   * `blocking`. Chosen by cost, never by a wish to hide the failure.
   */
  impact?: ErrorNoticeImpact
  /**
   * The notice sits inside a host that already frames it (a warning box), so
   * it draws no box of its own: a line with a thin rule above it, in its
   * level's colours. Block variant only.
   */
  nested?: boolean
  /**
   * The host's own buttons for this failure (Retry, Refresh), rendered in the
   * action row before "Ask the agent" with a real-button look. Pass `<button>`
   * elements (or components that render one) as direct children. Block
   * variant only.
   */
  actions?: ReactNode
  /**
   * Opt IN to the agent hand-off. **Defaults to `false`, and the direction of that
   * default is the safety property.**
   *
   * The hand-off navigates to the chat, which unmounts whatever rendered this
   * banner. Any value still living in that subtree's local state is destroyed —
   * and a save banner is, by definition, showing because the value was NOT
   * persisted. So an opt-OUT default makes *forgetting a prop* mean silent data
   * loss, on exactly the surfaces most likely to have a half-filled form. Opt-in
   * inverts that: forgetting the prop means "no button", which costs a
   * convenience and loses nothing.
   *
   * Set it `true` where there is nothing to lose — crash fallbacks, and read/list
   * failures on pages that hold no draft input. Leave it off next to any editable
   * field whose contents are not yet saved somewhere durable. Where the agent
   * the hand-off would open cannot run at all, pass `noHandoffReason` instead.
   */
  askAgent?: boolean
  /**
   * Scoped label for the hand-off link ("Ask the agent about this refusal").
   * When several notices coexist on one screen, identical default labels leave
   * the user unable to tell which link asks about which problem. Ignored when
   * `askAgent` is off.
   */
  askAgentLabel?: string
  /**
   * Inline variant only: `below-on-phone` wraps the row under the `sm`
   * breakpoint so the hand-off takes the line under the message. The block
   * variant always puts the hand-off in its action row under the message, so
   * it reads no placement. Ignored when `askAgent` is off.
   */
  actionPlacement?: 'beside' | 'below' | 'below-on-phone'
  /**
   * Inline variant only: `below` wraps the row so the message takes its own
   * smaller line under the `title`. The block variant always puts the message
   * under the title. Ignored without a `title`.
   */
  messagePlacement?: 'beside' | 'below'
  /**
   * Let the message scroll inside a height-constrained block notice while its
   * built-in hand-off stays visible in the action row. The host supplies the
   * height constraint. Off by default; ignored by the inline variant.
   */
  scrollMessage?: boolean
  /**
   * Rendered INSIDE the banner, under the message (block variant only) — for
   * a follow-on line that answers the message above it (a resolved outcome, a
   * next step). Outside the border it reads as a detached caption; inside,
   * the answer visibly belongs to the question. Import ReactNode consumers
   * pass plain elements; falsy renders nothing.
   */
  footer?: React.ReactNode
  /**
   * Forwarded to the hand-off button: runs only once the hand-off has actually
   * proceeded. For a notice rendered inside an OVERLAY that would otherwise sit
   * over the chat the hand-off navigates to (a modal, the remote-crew error
   * panel), so the caller can dismiss it — a hand-off the user cannot see reads
   * as a dead button. Ignored when `askAgent` is off.
   */
  onHandoff?: () => void
  /**
   * Stages the error's hand-off prompt in a composer the caller owns, in place
   * of the fresh-chat hand-off: for a surface bound to THIS chat's remote
   * workspace, which a fresh chat's agent could not reach. Nothing navigates.
   * Unset (every other caller), the hand-off opens a fresh chat as before.
   * Ignored when `askAgent` is off.
   */
  onStage?: (prompt: string) => void
  /**
   * Runs the hand-off through the host's own leave guard (`useGuardedLeave`'s
   * `leave`, forwarded to `AskAgentButton`): for a notice next to an unsaved
   * draft whose host HAS that guard. The guard asks before the navigation, so
   * the hand-off cannot discard the draft silently; a veto stages nothing.
   * Ignored when `askAgent` is off.
   */
  gate?: (proceed: () => void) => void
  className?: string
  /**
   * Classes for the `message` span only — e.g. `font-mono` when the message is
   * verbatim tool or server output. Scoped there, not on the root, so a
   * plain-language `title` keeps the UI font and reads as a separate clause
   * from the raw output beside it.
   */
  messageClassName?: string
  /**
   * Native `title` for the `message` span, for a call site that TRUNCATES the
   * message (`messageClassName="truncate"`) to hold a fixed row height. A clipped
   * error is unrecoverable without this: `role="alert"` reads the whole text to
   * assistive tech, but a sighted user sees only what fits, and for a server
   * sentence that is exactly the half naming what to do about it. Pass the full
   * message. Left unset, no tooltip is rendered -- an untruncated message needs
   * none, and a duplicate tooltip on a fully visible line is noise.
   */
  messageTooltip?: string
  /**
   * Ref to the `message` span, for a call site whose `messageClassName` bounds
   * the message and lets it scroll: the host measures that span to paint its
   * edge cue (`useScrollEdgesY`). Like `messageClassName`, it makes the block
   * variant wrap the message in that span.
   */
  messageRef?: React.Ref<HTMLSpanElement>
  /**
   * `data-testid` for the root element. Several notices can share one surface
   * (a page-level read failure above a row's own mutation failure), and a
   * shared `role="alert"` makes a lookup ambiguous — a call site that migrates
   * an existing `<p data-testid="…">` keeps its id here.
   */
  testId?: string
  /**
   * Off for a notice nested in a row that already marks the failure with its
   * own status icon (a pre-flight check row's warning circle), so the problem
   * is not shown twice. The icon is decorative (`aria-hidden`), so turning it
   * off changes nothing for assistive tech; `role="alert"` stays. On by default.
   */
  showIcon?: boolean
  /**
   * A short visible line saying what gets the hand-off back, shown in its place
   * while `askAgent` is off -- for a notice whose hand-off is withheld only for
   * now (a sibling list holds unapplied edits the navigation would drop). A
   * withheld button with no word about it reads as a missing feature. Muted, not
   * the alert's colour: it is a next step, not a second failure. Ignored while
   * `askAgent` is on; unset, nothing renders.
   */
  handoffHint?: string
  /**
   * Off for a notice whose text a SHARED live region already announces — the
   * floating agent-switch toast carries the same string, so a second assertive
   * region reads the failure out twice. `showIcon` is the visual half of this
   * and says `role="alert"` stays; this is the half that drops it. Only legal
   * where the notice is never the sole surface: the in-place copy stays
   * readable, it just no longer interrupts. On by default.
   */
  announce?: boolean
} & ErrorNoticeNoHandoff) {
  // Subscribes to language switches: a parent compiled by the React Compiler
  // caches this element while its props are unchanged, so without its own
  // subscription the notice would keep the previous language's text.
  useLanguageGeneration()
  if (!message) return null
  const level = LEVELS[impact]
  const LevelIcon = level.Icon
  const ink = nested ? level.inkNested : level.ink
  const body = nested ? level.bodyNested : level.body
  const link = impact === 'minor' ? 'text-minor-link' : ink
  // Blocking interrupts; a partial or minor failure is read politely.
  const role = announce ? (impact === 'blocking' ? 'alert' : 'status') : undefined

  // One name for the dismiss control, read two ways: `aria-label` for the
  // accessibility tree and `title` as the tooltip, so a sighted user hovering
  // the icon-only ✕ sees the same promise a screen reader announces.
  const dismissName = dismissLabel ?? i18nT('components.errorNotice.dismiss')
  const handoff = askAgent && (
    <AskAgentButton
      report={report}
      message={message}
      onHandoff={onHandoff}
      onStage={onStage}
      gate={gate}
      label={askAgentLabel}
      tone="inherit"
    />
  )

  if (variant === 'inline') {
    // The secondary line: smaller than the title, in the level's body colour.
    const messageBelow = messagePlacement === 'below' && Boolean(title)
    // Below `sm` the row wraps so the hand-off takes the line under the message.
    const phoneBelow = askAgent && actionPlacement === 'below-on-phone' && !messageBelow
    return (
      <span
        role={role}
        className={`inline-flex items-center gap-1.5 text-[12px] ${ink} ${messageBelow ? 'flex-wrap' : ''}${phoneBelow ? ' flex-wrap sm:flex-nowrap' : ''} ${className}`}
        id={id}
        data-testid={testId}
      >
        {showIcon && <LevelIcon size={14} className="shrink-0" aria-hidden="true" />}
        {title && <strong className={`min-w-0 ${impact === 'minor' ? level.title : 'font-semibold'}`} style={{ overflowWrap: 'anywhere' }}>{title}</strong>}
        <span ref={messageRef} className={`min-w-0 ${messageBelow ? `basis-full text-[11px] font-normal ${body}` : ''}${phoneBelow ? ' grow basis-[calc(100%-2rem)] sm:grow-0 sm:basis-auto' : ''} ${messageClassName}`} style={{ overflowWrap: 'anywhere' }} title={messageTooltip}>{withOriginLink(message)}</span>
        {/* shrink-0: the button inside never shrinks, so a wrapper that did
            would let the hand-off overflow the row; the message wraps instead. */}
        {handoff && <span className={`inline-flex shrink-0 ${link}`}>{handoff}</span>}
        {!askAgent && handoffHint && (
          <span className="min-w-0 text-[11px] font-normal text-muted" data-testid="error-notice-handoff-hint">{handoffHint}</span>
        )}
        {onDismiss && (
          <button
            type="button"
            className={`shrink-0 bg-transparent border-none p-0 cursor-pointer text-muted hover:text-text-strong transition-colors${dismissLabelVisible ? ' inline-flex items-center gap-1 text-[12px] font-medium whitespace-nowrap' : ''}`}
            aria-label={dismissName}
            title={dismissName}
            onClick={onDismiss}
          >
            <X size={13} aria-hidden="true" />
            {dismissLabelVisible && <span>{dismissName}</span>}
          </button>
        )}
      </span>
    )
  }

  const dismissButton = onDismiss && (
    <button
      type="button"
      className={`bg-transparent border-none p-0 cursor-pointer text-muted hover:text-text-strong transition-colors${dismissLabelVisible ? ' inline-flex items-center gap-1 text-[12px] font-medium min-w-0' : ' shrink-0 mt-[2px]'}`}
      aria-label={dismissName}
      title={dismissName}
      onClick={onDismiss}
    >
      <X size={14} aria-hidden="true" />
      {dismissLabelVisible && <span>{dismissName}</span>}
    </button>
  )
  // A minor notice is always a line; a nested one sits on its host's fill.
  const framed = impact !== 'minor' && !nested
  const shell = framed
    ? `rounded-lg ${level.box} px-3 py-2.5`
    : nested ? `border-t ${level.rule} pt-2` : ''
  const small = !framed
  const hasActions = Boolean(actions) || Boolean(askAgent) || Boolean(dismissLabelVisible && dismissButton)

  return (
    <div
      role={role}
      className={`${shell} ${small ? 'text-[12.5px] leading-[1.45]' : 'text-[13px] leading-[1.5]'}${scrollMessage ? ' flex flex-col' : ''} ${className}`}
      id={id}
      data-testid={testId}
    >
      <div className={`flex items-start gap-2${scrollMessage ? ' min-h-0 flex-1' : ''}`}>
        {showIcon && <LevelIcon size={small ? 14 : 15} className={`mt-[2px] shrink-0 ${ink}`} aria-hidden="true" />}
        <div className={`min-w-0 flex-1${scrollMessage ? ' min-h-0 self-stretch flex flex-col' : ''}`} style={{ overflowWrap: 'anywhere' }}>
          {/* The trailing space keeps the title and message apart in the
              text content a screen reader or a test reads as one run. */}
          {title && <strong className={`block ${ink} ${level.title}`}>{title} </strong>}
          <span
            ref={messageRef}
            className={`block max-w-[68ch] whitespace-pre-wrap ${body}${title ? ' mt-0.5' : ''}${scrollMessage ? ' min-h-0 overflow-y-auto' : ''} ${messageClassName}`}
            title={messageTooltip}
          >
            {withOriginLink(message)}
          </span>
          {footer && <div className={`mt-1 ${body}`}>{footer}</div>}
          {!askAgent && handoffHint && (
            <div className="mt-1 text-[12px] font-normal text-muted" data-testid="error-notice-handoff-hint">{handoffHint}</div>
          )}
          {hasActions && (
            <div data-notice-actions="" className={`mt-2 flex flex-wrap items-center gap-x-4 gap-y-2${scrollMessage ? ' shrink-0' : ''} ${ACTION_BUTTONS}`}>
              {actions}
              {handoff && <span className={`inline-flex ${link}`}>{handoff}</span>}
              {dismissLabelVisible && dismissButton && <span className="ml-auto inline-flex">{dismissButton}</span>}
            </div>
          )}
        </div>
        {!dismissLabelVisible && dismissButton}
      </div>
    </div>
  )
}
