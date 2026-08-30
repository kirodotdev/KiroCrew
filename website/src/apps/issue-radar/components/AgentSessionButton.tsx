import { Loader, Check, RotateCcw, type LucideIcon } from 'lucide-react'
import type { InvestigationRecord } from '../api'

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
  disabled = false, concluded = false, onStartOver,
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
}) {
  const hasSession = !!record?.slot_key
  const resolved = record?.status === 'resolved'
  const verdict = record?.findings?.verdict
  const summary = record?.findings?.summary
  // A declined click turns THIS button into the re-run rather than adding a
  // second control or a sentence beside it. Two reasons, and the second is why
  // there is no inline notice at all:
  //
  // Layout: the detail header wraps these in a `flex-shrink-0` group whose own
  // comment says it "holds buttons only" -- a text node there cannot shrink, so
  // it pushes the row past a 320px pane (`website/docs/page-layout.md`), and a
  // third button would breach `AUTOSDE.yaml`'s `max-two-buttons-per-row`.
  //
  // Redundancy: the row ALREADY says the work finished. The status pill beside
  // this button carries the recorded verdict, and the label flipping from
  // "Resume" to "Start over" says what a further click would do. A sentence
  // repeating "already finished" adds a third element saying what those two
  // already say together; the full wording stays on the button's title for
  // anyone who wants the reason spelled out.
  const noticeText = i18nT('apps.issueRadar.components.agentSessionButton.already_finished_its_session_was_closed')
  const actionLabel = concluded
    ? i18nT('apps.issueRadar.components.agentSessionButton.start_over')
    : (hasSession ? i18nT('apps.issueRadar.components.agentSessionButton.resume') : label)
  // The icon changes WITH the label, so the flip is not text-only. The second
  // click lands on the same pixel as the first, so a user who reads the relabel
  // as "nothing happened" would click again and spend a fresh agent run; two
  // changing glyphs are harder to miss than one changed word. Cheap on purpose --
  // an added element is what the action group's buttons-only, `flex-shrink-0`
  // contract rules out.
  const ActionIcon = concluded ? RotateCcw : Icon

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        onClick={concluded ? onStartOver : onClick}
        disabled={busy || disabled}
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
          ? <Loader size={13} className="animate-spin" />
          : <ActionIcon size={13} />}
        {actionLabel}
      </button>

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

      {concluded && (
        // Announced, not just hovered. The reason otherwise lives only in the
        // button's `title`, which a keyboard or screen-reader user activating
        // Resume never receives -- they get a label that quietly changed and no
        // account of why nothing resumed. `sr-only` is absolutely positioned and
        // clipped, so this adds nothing to the action row's width, which is what
        // the group's `flex-shrink-0` buttons-only contract actually rules out.
        <span role="status" className="sr-only">{noticeText}</span>
      )}

      {error && (
        <span className="text-[10.5px] text-danger" title={error.message}>
          {i18nT('apps.issueRadar.components.agentSessionButton.couldn_t_start')}
        </span>
      )}
    </span>
  )
}
