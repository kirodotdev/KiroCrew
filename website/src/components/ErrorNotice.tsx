import type { ComponentType, ReactNode } from 'react'
import { AlertTriangle, CircleAlert, Sparkles, X } from 'lucide-react'
import AskAgentButton, { handoffErrorToAgent } from './AskAgentButton'
import type { ErrorReport } from '../utils/errorReport'

import { i18nT } from '../i18n/t'
import { withOriginLink } from './withOriginLink'

export type ErrorNoticeMenuItemComponent = ComponentType<{
  title?: string
  disabled?: boolean
  'aria-describedby'?: string
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
 */
export function ErrorNoticeMenuItem({
  Item,
  message,
  describedBy,
  outcome,
  report,
}: {
  Item: ErrorNoticeMenuItemComponent
  message?: string | null
  describedBy: string
  outcome?: string
  /** The structured report, for a host whose `message` is its own rewording
   *  and so differs from the journal entry `findReport` looks up. */
  report?: ErrorReport
}) {
  if (!message) return null

  return (
    <Item
      title={i18nT('components.askAgent.open_a_chat_with_this_error_s_context_attached')}
      aria-describedby={describedBy}
      onSelect={(event) => {
        if (!handoffErrorToAgent({ message, report })) event.preventDefault()
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
 * ## `warn` is severity, and it is a separate axis from `variant`
 *
 * Danger chrome is read before the words are, so a notice reporting a PARTIAL
 * SUCCESS in red alarm colours tells a scanning user the operation failed and
 * invites them to re-run it. `warn` is for that case — something was withheld,
 * nothing broke. Leave it off (the default) for a genuine failure.
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
  warn = false,
  askAgent = false,
  askAgentLabel,
  actionPlacement = 'beside',
  messagePlacement = 'beside',
  footer,
  onHandoff,
  className = '',
  messageClassName = '',
  messageTooltip,
  testId,
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
   * only ever removes the notice; there is no toned-down or muted register
   * for an error (a failure toned down to a polite status is still a failure
   * — see `errors-use-error-notice`), so a caller that wants the notice gone
   * hides it, and one that wants it seen renders it exactly like this.
   */
  dismissLabel?: string
  /**
   * Show `dismissLabel` as VISIBLE text beside the ✕ -- the same one string,
   * read three ways (text, accessible name, tooltip) -- when dismissing is a
   * step in the user's task rather than the closing of a notice ("Dismiss
   * notice and re-enable Install" on a banner whose dismissal re-enables the
   * Install button), so the path back is discoverable without reading the
   * paragraph that mentions it. Off by default: every other consumer keeps the
   * icon-only ✕ exactly as it renders today. Ignored without `onDismiss`. In
   * the block variant a control with text is wide, so the notice's row wraps
   * and the text takes a line of its own below the message at narrow widths.
   */
  dismissLabelVisible?: boolean
  /** `block` = boxed banner; `inline` = compact text for an existing flex row. */
  variant?: 'block' | 'inline'
  /**
   * Severity, independent of `variant`. On for an outcome that withheld
   * something without failing (a partial clear); off for a real failure.
   */
  warn?: boolean
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
   * field whose contents are not yet saved somewhere durable.
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
   * Where the hand-off sits in the block variant. `beside` (default) puts it in
   * the banner's right-hand column, which is right for a banner that spans a
   * page. `below` stacks it under the text, inside the text column: in a
   * NARROW host — the chat sidebar is ~300px — a sibling column takes a third
   * of the width and the title and message wrap one or two words per line. Not
   * a container query: jsdom cannot evaluate one, so the pin would be
   * untestable, and `container-type` on the shared root would collapse a
   * notice laid out in a shrink-to-fit context. Ignored by the inline variant
   * and when `askAgent` is off.
   */
  actionPlacement?: 'beside' | 'below'
  /**
   * Where the `message` sits relative to the `title`. `beside` (default) runs
   * the two as one sentence -- right when the message is the human-readable
   * clause ("Save failed: the folder no longer exists"). `below` puts the message
   * on its own line under the title, smaller and secondary: for a notice whose
   * `message` is a raw server string kept because it is the journal lookup key
   * (the hand-off recovers endpoint and status from it) while the plain-language
   * `title` carries the meaning -- "config store" and "gateway" mean nothing to
   * a first-time reader, so they read as a detail, not as the lead. In the
   * inline variant the row wraps to make the line. Ignored without a `title`.
   */
  messagePlacement?: 'beside' | 'below'
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
   * `data-testid` for the root element. Several notices can share one surface
   * (a page-level read failure above a row's own mutation failure), and a
   * shared `role="alert"` makes a lookup ambiguous — a call site that migrates
   * an existing `<p data-testid="…">` keeps its id here.
   */
  testId?: string
}) {
  if (!message) return null
  // The secondary line: smaller than the title, lighter than the lead, but still
  // the alert's own colour -- it is the failure's text, demoted, not a caption.
  const messageBelow = messagePlacement === 'below' && Boolean(title)

  // One name for the dismiss control, read two ways: `aria-label` for the
  // accessibility tree and `title` as the tooltip, so a sighted user hovering
  // the icon-only ✕ sees the same promise a screen reader announces.
  const dismissName = dismissLabel ?? i18nT('components.errorNotice.dismiss')

  const fg = warn ? 'text-warn' : 'text-danger'
  // Politeness rides the SEVERITY axis rather than a prop: a withheld action broke nothing,
  // so interrupting speech misreports it -- and per-site roles drifted apart once already.
  const role = warn ? 'status' : 'alert'
  const dismissFg = warn ? 'text-warn/70 hover:text-warn' : 'text-danger/70 hover:text-danger'
  // Warn ranks BELOW danger: a withheld act lost nothing, so it must not out-shout a real
  // failure. Circle < triangle in weight, and danger's triangle is left as every surface has it.
  const Icon = warn ? CircleAlert : AlertTriangle

  if (variant === 'inline') {
    return (
      <span
        role={role}
        className={`inline-flex items-center gap-1.5 text-[12px] ${fg} ${messageBelow ? 'flex-wrap' : ''} ${className}`}
        id={id}
        data-testid={testId}
      >
        <Icon size={14} className="shrink-0" aria-hidden="true" />
        {title && <strong className="font-semibold">{title}</strong>}
        <span className={`min-w-0 ${messageBelow ? 'basis-full text-[11px] font-normal text-danger/80' : ''} ${messageClassName}`} style={{ overflowWrap: 'anywhere' }} title={messageTooltip}>{withOriginLink(message)}</span>
        {askAgent && (
          <AskAgentButton
            report={report}
            message={message}
            onHandoff={onHandoff}
            label={askAgentLabel}
          />
        )}
        {onDismiss && (
          <button
            type="button"
            className={`shrink-0 bg-transparent border-none p-0 cursor-pointer ${dismissFg} transition-colors${dismissLabelVisible ? ' inline-flex items-center gap-1 text-[12px] font-medium whitespace-nowrap' : ''}`}
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

  // A dismiss whose name is shown as text (`dismissLabelVisible`) is a wide
  // control -- "Dismiss notice and re-enable Install" is ~230px at 12px, and the
  // German and French strings are longer -- and it shares the row with the
  // message and the agent hand-off. At 320px the three cannot stand side by
  // side: the label was `whitespace-nowrap`, so the message column was crushed
  // to nothing and the row overflowed the notice. Same cure as
  // CrashReportNotice: `flex-wrap` -> `md:flex-nowrap` on the row, and the
  // control takes a full-width line of its own (`basis-full`, right-aligned)
  // below the message at narrow widths, rejoining the row at `md`. That line is
  // the notice's content width, and the German label (~300px at 12px) is wider
  // than it at 320px, so below `md` the button may shrink and its label wrap:
  // `shrink-0` and `whitespace-nowrap` hold only from `md` up, where the row has
  // room for the whole label on one line. One DOM node for the action at every
  // width -- never a hidden/shown pair, which would make
  // `getByRole('button', { name })` match twice. The icon-only ✕ keeps the
  // single-row shape every existing consumer's tests read.
  const dismissButton = onDismiss && (
    <button
      type="button"
      className={`${dismissLabelVisible ? '' : 'shrink-0 '}bg-transparent border-none p-0 cursor-pointer text-danger/70 hover:text-danger transition-colors${dismissLabelVisible ? ' inline-flex items-center gap-1 mt-[1px] text-[12px] font-medium min-w-0 md:shrink-0 md:whitespace-nowrap' : ''}`}
      aria-label={dismissName}
      title={dismissName}
      onClick={onDismiss}
    >
      <X size={14} aria-hidden="true" />
      {dismissLabelVisible && <span>{dismissName}</span>}
    </button>
  )

  return (
    <div
      role={role}
      className={`rounded-lg border ${warn ? 'border-warn/40 bg-warn/10' : 'border-danger/40 bg-danger/10'} px-3 py-2 flex ${dismissLabelVisible ? 'flex-wrap md:flex-nowrap ' : ''}items-start gap-2 text-[13px] ${fg} ${className}`}
      id={id}
      data-testid={testId}
    >
      <Icon size={14} className="mt-[2px] shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1 whitespace-pre-wrap" style={{ overflowWrap: 'anywhere' }}>
        {title && <strong className="font-semibold">{title} </strong>}
        {/* Wrapped only when asked: the bare text node is the shape every
            existing consumer's tests read. */}
        {messageBelow || messageClassName || messageTooltip
          ? <span className={`${messageBelow ? 'block text-[12px] font-normal text-danger/80' : ''} ${messageClassName}`} title={messageTooltip}>{withOriginLink(message)}</span>
          : withOriginLink(message)}
        {footer && <div className="mt-1 font-normal">{footer}</div>}
        {askAgent && actionPlacement === 'below' && (
          <div className="mt-1.5">
            <AskAgentButton
              report={report}
              message={message}
              onHandoff={onHandoff}
              label={askAgentLabel}
            />
          </div>
        )}
      </div>
      {askAgent && actionPlacement === 'beside' && (
        <AskAgentButton
          report={report}
          message={message}
          onHandoff={onHandoff}
          label={askAgentLabel}
          className="mt-[1px]"
        />
      )}
      {dismissLabelVisible
        ? dismissButton && (
          <span className="basis-full md:basis-auto shrink-0 flex justify-end">
            {dismissButton}
          </span>
        )
        : dismissButton}
    </div>
  )
}
