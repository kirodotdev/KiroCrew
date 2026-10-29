import { Pause, Play } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { FINISHED_STOP_REASONS, intervalText } from '../../components/autoNudgeLoop'
import { fmtDateTimeNumeric } from '../../i18n/format'
import { cn } from '../../lib/utils'
import type { AutomationRecord, LegacyGoalLoop } from '../../monitoring/automation'
import { timeAgo } from '../../utils/timeAgo'
import { inDuration } from './CrewScheduleList'

/**
 * The patrol a crewmate runs over the tasks it handed out: the `work-ledger`
 * goal loop the conductor prompt (or the gateway's default patrol after a bind)
 * arms on the crewmate's own slot. Active, or retained after a stop so the person
 * can see WHY it went quiet and resume it. Anything else on the slot -- a pull
 * request watch, a plain timer -- is the chat composer's popover's business and
 * is not a row here.
 */
export function patrolLoopOf(record: AutomationRecord | null | undefined): LegacyGoalLoop | null {
  if (!record || record.kind !== 'legacy_goal_loop' || record.monitorKind !== 'work-ledger') return null
  // A patrol that FINISHED (every item closed, or its stop file dropped) has
  // nothing left to watch and the service refuses to revive it, so a row with
  // Resume would offer a control that does nothing: it is no row at all.
  if (!record.active && FINISHED_STOP_REASONS.has(record.stoppedReason)) return null
  return record.active || record.stoppedReason ? record : null
}

/** The one-phrase reason a stopped patrol shows. Stop codes outside the four a
 *  person can act on read as the plain "stopped": the budget bookkeeping behind
 *  them is a guardrail, not information, and naming the code would send a
 *  reader to look it up. */
const STOP_KEYS: Readonly<Record<string, string>> = {
  manual: 'pages.membersPage.patrol_stopped_manual',
  user_stop: 'pages.membersPage.patrol_stopped_manual',
  runtime_budget: 'pages.membersPage.patrol_stopped_runtime',
  cycle_cap: 'pages.membersPage.patrol_stopped_cycles',
  approval_stalled: 'pages.membersPage.patrol_stopped_approval',
  approval_stall: 'pages.membersPage.patrol_stopped_approval',
  session_close: 'pages.membersPage.patrol_stopped_session',
}

/** Stop codes that mean the patrol FAILED rather than ran out or was paused:
 *  these are errors and render through `ErrorNotice` under the row, each with
 *  its next step (Resume), so the cause is not hidden behind "Stopped". */
const FAILURE_STOP_KEYS: Readonly<Record<string, string>> = {
  session_start_failures: 'pages.membersPage.patrol_failed_start',
  session_unavailable: 'pages.membersPage.patrol_failed_start',
  structural_terminal: 'pages.membersPage.patrol_failed_check',
  model_refused: 'pages.membersPage.patrol_failed_check',
  interrupted_cycle: 'pages.membersPage.patrol_failed_check',
  consecutive_failures: 'pages.membersPage.patrol_failed_check',
  // The service stopped the loop because it could no longer run it as armed:
  // its stop file failed revalidation on load, its verdict never settled, its
  // record was unreadable or from a newer schema.
  sentinel_dropped: 'pages.membersPage.patrol_failed_blocked',
  verdict_stall: 'pages.membersPage.patrol_failed_blocked',
  completion_evidence_unavailable: 'pages.membersPage.patrol_failed_blocked',
  invalid_monitor_record: 'pages.membersPage.patrol_failed_blocked',
  unsupported_monitor_version: 'pages.membersPage.patrol_failed_blocked',
}

/**
 * One row under the Schedules tab's "On its own" header: the patrol, in the
 * schedule row's grammar (name, state pill, what it does, last/next). Unlike a
 * schedule it does not open a detail page: its one control is here, Pause while
 * it runs and Resume once it stopped, because there is nothing else to edit.
 *
 * Presentational: the host owns the loop read, the open-task count (the crew
 * board) and the write, so the card stays testable without providers.
 */
export default function CrewOnItsOwn({ loop, openTasks, nowTs, busy, error, countFailed = false, onPause, onResume }: {
  loop: LegacyGoalLoop
  /** Open items on the crewmate's board; `null` while unknown (no ledger, or unread). */
  openTasks: number | null
  nowTs: number
  busy: boolean
  /** The last write's refusal, '' when none. */
  error: string
  /** The open-task count could not be read (a failure other than "no ledger"). */
  countFailed?: boolean
  onPause: () => void
  onResume: () => void
}) {
  const { t } = useTranslation()
  const active = loop.active
  const interval = intervalText(loop.idleSecs)
  const lastAt = loop.lastFireAt
  const nextAt = loop.nextDueAt ?? 0
  const stopKey = STOP_KEYS[loop.stoppedReason]
  const failureKey = active ? undefined : FAILURE_STOP_KEYS[loop.stoppedReason]
  const facts: string[] = []
  if (active) {
    facts.push(lastAt > 0
      ? t('pages.membersPage.patrol_last_check', { when: timeAgo(lastAt) })
      : t('pages.membersPage.patrol_not_checked'))
    // A next check in the past is not a promise this row can make: the loop is
    // late, and saying "next in 0m" would read as imminent.
    if (nextAt > nowTs) facts.push(t('pages.membersPage.schedule_next_run', { time: inDuration(nextAt - nowTs) }))
  } else if (!failureKey) {
    facts.push(stopKey
      ? t(stopKey, { limit: inDuration(loop.maxRuntimeSecs ?? 0) })
      : t('pages.membersPage.patrol_stopped_other'))
  }
  if (openTasks !== null) {
    facts.push(t(active ? 'pages.membersPage.patrol_tasks_open' : 'pages.membersPage.patrol_tasks_still_open', { count: openTasks }))
  }

  return (
    <section className="flex flex-col gap-2" data-testid="crew-on-its-own">
      <ul className="list-none m-0 p-0 rounded-2xl border border-border bg-bg overflow-hidden">
        <li
          className="flex items-start gap-3 px-3.5 py-3"
          data-testid="crew-patrol-row"
          data-state={active ? 'running' : 'paused'}
        >
          <span className="flex-1 min-w-0 leading-tight">
            <span className="flex items-center gap-2 min-w-0">
              <span className="text-[13.5px] font-semibold truncate">{t('pages.membersPage.patrol_name')}</span>
              <span
                className={cn(
                  'ml-auto shrink-0 text-[10.5px] font-semibold px-2 py-0.5 rounded-full',
                  active ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted',
                )}
                data-testid="crew-patrol-state"
              >
                {t(active ? 'pages.membersPage.schedule_state_running' : 'pages.membersPage.schedule_state_paused')}
              </span>
            </span>
            <span className="block text-[12.5px] text-text mt-0.5">{t('pages.membersPage.patrol_wakes', { interval })}</span>
            <span
              className="block text-[11.5px] text-muted mt-1"
              title={lastAt > 0 ? fmtDateTimeNumeric(lastAt) : undefined}
              data-testid="crew-patrol-facts"
            >
              {facts.join(' · ')}
            </span>
          </span>
          <Btn
            type="button"
            primary={!active}
            className="shrink-0 rounded-full px-3 h-8"
            onClick={active ? onPause : onResume}
            disabled={busy}
            data-testid="crew-patrol-toggle"
          >
            {active ? <Pause size={13} aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}
            {t(active ? 'pages.membersPage.patrol_pause' : 'pages.membersPage.patrol_resume')}
          </Btn>
        </li>
      </ul>
      {/* No hand-off on either: the Members page may hold an unsaved schedule
          or crew-editor draft, and Ask the agent navigates away from it. */}
      {failureKey && <ErrorNotice message={t(failureKey)} testId="crew-patrol-stop-error" />}
      {countFailed && <ErrorNotice message={t('pages.membersPage.patrol_count_failed')} testId="crew-patrol-count-error" />}
      {error && <ErrorNotice message={error} testId="crew-patrol-error" />}
    </section>
  )
}
