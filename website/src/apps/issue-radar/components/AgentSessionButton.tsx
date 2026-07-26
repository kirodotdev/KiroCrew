import { Loader2, Check, type LucideIcon } from 'lucide-react'
import type { InvestigationRecord } from '../api'

/** Presentation for the issue "Investigate" and PR "Review" header controls —
 * identical in every respect except their icon and labels, so the markup lives
 * here once. Reflects the item's saved record:
 *   * no record   → the primary label ("Investigate" / "Review")
 *   * has session → "Resume" + a status pill (in-progress / done + verdict)
 * The owning component supplies the click handler and busy/error state. */
export default function AgentSessionButton({
  icon: Icon, label, record, busy, error, onClick,
  startHint, resumeHint, pendingLabel, donePillLabel, showStatus = true,
  disabled = false,
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
}) {
  const hasSession = !!record?.slot_key
  const resolved = record?.status === 'resolved'
  const verdict = record?.findings?.verdict
  const summary = record?.findings?.summary

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        onClick={onClick}
        disabled={busy || disabled}
        title={hasSession ? resumeHint : startHint}
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
          : <Icon size={13} />}
        {hasSession ? 'Resume' : label}
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

      {error && (
        <span className="text-[10.5px] text-danger" title={error.message}>
          couldn't start
        </span>
      )}
    </span>
  )
}
