import { useEffect, useState } from 'react'
import { Loader2, Check, RotateCcw, Clock, type LucideIcon } from 'lucide-react'
import type { InvestigationRecord } from '../api'
import { Popover, PopoverAnchor, PopoverContent } from '../../../components/ui/popover'

import { i18nT } from '../../../i18n/t'
/** Presentation for the issue "Investigate" and PR "Review" header controls —
 * identical in every respect except their icon and labels, so the markup lives
 * here once. Reflects the item's saved record:
 *   * no record   → the primary label ("Investigate" / "Review")
 *   * has session → "Resume" + a status pill (in-progress / done + verdict)
 * The owning component supplies the click handler and busy/error state. */
export default function AgentSessionButton({
  icon: Icon, label, record, busy, error, onClick,
  startHint, resumeHint, pendingLabel, donePillLabel, showStatus = true,
  disabled = false, concluded = false, onStartOver, onOpenOlderSessions,
}: {
  icon: LucideIcon
  /** Label shown when there is no session yet. */
  label: string
  record: InvestigationRecord | null
  busy: boolean
  error: Error | null
  onClick: () => void
  /** Tooltip when no session exists yet. */
  startHint: string
  /** Tooltip when resuming an existing session. */
  resumeHint: string
  /** Status pill text while the work is in progress. */
  pendingLabel?: string
  /** Status pill text when finished and no verdict was recorded. */
  donePillLabel?: string
  /** Render the status pill at all. Turn OFF when the session's agent is not
   * asked to write a result back — the pill would then be stuck on "pending"
   * forever, which is worse than showing no status. */
  showStatus?: boolean
  /** Block the action for a reason other than being busy — e.g. the owning
   * component cannot yet tell whether a session already exists, and guessing
   * would create a duplicate. */
  disabled?: boolean
  /** The click was declined because the item's work already concluded and its
   * session is gone. Renders an explanation plus the explicit re-run below,
   * INSTEAD of silently starting the finished work over. */
  concluded?: boolean
  /** Start over anyway. Wired to the notice's own action, so re-doing concluded
   * work always takes a second, deliberate click. */
  onStartOver?: () => void
  /** Go to the chat page's Older Sessions pane, where a closed session's
   * transcript is. Supplied by the owning component because navigation belongs
   * to the session layer, not to shared presentation. */
  onOpenOlderSessions?: () => void
}) {
  const hasSession = !!record?.slot_key
  const resolved = record?.status === 'resolved'
  const verdict = record?.findings?.verdict
  const summary = record?.findings?.summary
  // A declined click turns THIS button into the re-run, AND says so in a popover
  // anchored to it.
  //
  // WHY A POPOVER RATHER THAN A LINE IN THE ROW
  //
  // The reason used to live only in this button's `title` plus an `sr-only` live
  // region. Between them those serve a hovering mouse user and a screen-reader
  // user; a sighted KEYBOARD user is in neither group (browsers surface `title` on
  // hover, not on focus) and a touch user cannot surface a `title` at all — which
  // is the population a declined click leaves with no explanation whatsoever.
  //
  // Two earlier attempts put the sentence in the toolbar row and both were
  // correctly blocked. The constraint is narrower than "buttons only" — this very
  // component already renders inline text for its error branch — it is WRAPPING:
  // the group is `flex-shrink-0 flex items-stretch` (`DetailHeader.tsx`), so a
  // sibling that wraps stretches the button to its height, and this sentence wraps
  // at 320px (`website/docs/page-layout.md`: "verify at 320px"). A popover is
  // portalled OUT of that flex row, so neither the wrapping nor the stretching
  // applies and the constraint does not reach it.
  const noticeText = i18nT('apps.issueRadar.components.agentSessionButton.already_finished_its_session_was_closed')
  const actionLabel = concluded
    ? i18nT('apps.issueRadar.components.agentSessionButton.start_over')
    : (hasSession ? i18nT('apps.issueRadar.components.agentSessionButton.resume') : label)
  // The icon changes WITH the label, so the flip is not text-only. Cheap, and
  // still worth keeping now that the popover explains the decline: the label and
  // icon are what remain after the popover is dismissed.
  const ActionIcon = concluded ? RotateCcw : Icon

  // Dismissal is local, so Escape or a click outside closes the notice without
  // clearing `concluded` — the label must stay "Start over" until the re-run
  // actually happens. Reset when `concluded` rises, so a later decline (this item
  // again, or another one after the pane is reused) shows the notice afresh
  // instead of inheriting an earlier dismissal.
  const [dismissed, setDismissed] = useState(false)
  useEffect(() => { if (concluded) setDismissed(false) }, [concluded])
  const noticeOpen = concluded && !dismissed

  return (
    <Popover open={noticeOpen} onOpenChange={(open) => { if (!open) setDismissed(true) }}>
      <span data-testid="agent-session-action-row" className="inline-flex items-center gap-1.5">
        <PopoverAnchor asChild>
          <button
            onClick={concluded ? onStartOver : onClick}
            disabled={busy || disabled}
            // Kept as a residual affordance for a pointer user who has dismissed
            // the popover. It is no longer the ONLY route to the reason, which is
            // what made it a defect.
            title={concluded ? noticeText : (hasSession ? resumeHint : startHint)}
            className={
              // Solid/filled, not a ghost outline: these are the pane's primary
              // actions, so they carry the design system's accent fill (the same
              // bg-accent / text-accent-fg / hover:bg-accent-hover triple used for
              // primary buttons elsewhere) instead of blending into the header.
              'inline-flex items-center gap-1 text-[12px] px-2.5 py-1 rounded-md border-none font-medium ' +
              'bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default ' +
              'cursor-pointer whitespace-nowrap transition-colors'
            }
          >
            {busy
              ? <Loader2 size={13} className="animate-spin" />
              : <ActionIcon size={13} />}
            {actionLabel}
          </button>
        </PopoverAnchor>

        {showStatus && record && (
          <span
            title={summary || (resolved ? `${donePillLabel}` : pendingLabel)}
            className={
              'text-[10.5px] px-1.5 py-0.5 rounded-full font-medium ' +
              (resolved ? 'bg-aim-subtle text-aim' : 'bg-accent-subtle text-accent')
            }
          >
            {resolved
              ? (verdict ? <><Check size={10} className="lucide-inline" /> {verdict}</> : donePillLabel)
              : pendingLabel}
          </span>
        )}

        {error && (
          <span className="text-[10.5px] text-danger" title={error.message}>
            {i18nT('apps.issueRadar.components.agentSessionButton.couldn_t_start')}
          </span>
        )}
      </span>

      {/* Narrower than the wrapper's default 288px when the viewport cannot spare
          it, so the notice is fully on screen at 320px rather than clipped by the
          collision boundary. `align="start"` puts its leading edge under the
          button that raised it — with `end` the 288px box is wider than the space
          to the anchor's left, so collision handling pushes it to the pane edge
          and it stops reading as attached to anything. */}
      <PopoverContent
        align="start"
        collisionPadding={8}
        className="w-[min(18rem,calc(100vw-1.5rem))] p-3 space-y-2"
      >
        {/* Announced as well as shown. A screen reader reaches this by focus (the
            popover is a dialog and takes focus on open); the live role covers the
            case where it does not move. */}
        <p role="status" className="text-[12px] leading-snug text-text">
          {noticeText}
        </p>
        {onOpenOlderSessions && (
          // The point of the whole change: the copy names a destination, so the
          // destination has to be something the user can go to. Clicking Resume on
          // finished work means "show me the result" — offering only "redo it" is
          // what made the old wording hollow.
          <button
            type="button"
            onClick={onOpenOlderSessions}
            className={
              'inline-flex items-center gap-1.5 text-[12px] font-medium px-2 py-1 rounded-md ' +
              'border border-border bg-transparent text-text hover:bg-bg-hover ' +
              'cursor-pointer transition-colors'
            }
          >
            <Clock size={12} className="shrink-0" />
            {i18nT('apps.issueRadar.components.agentSessionButton.open_older_sessions')}
          </button>
        )}
      </PopoverContent>
    </Popover>
  )
}
