import { useTranslation } from 'react-i18next'

import ErrorNotice from '../../components/ErrorNotice'
import { intervalText, PULL_REQUEST_WATCH_KINDS } from '../../components/autoNudgeLoop'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { isAutomationActive, type AutomationRecord } from '../../monitoring/automation'

export type MonitorReadState = 'loading' | 'ready' | 'error'

/** Profile's localized projection of persisted loop and monitor stop codes. */
const PROFILE_STOP_REASON_KEYS: Readonly<Record<string, string>> = {
  manual: 'pages.membersPage.profile_monitor_reason_manual',
  user_stop: 'pages.membersPage.profile_monitor_reason_manual',
  autonudge_stop: 'pages.membersPage.profile_monitor_reason_agent',
  cycle_cap: 'pages.membersPage.profile_monitor_reason_cycles',
  runtime_budget: 'pages.membersPage.profile_monitor_reason_runtime',
  agent_turn_budget: 'pages.membersPage.profile_monitor_reason_budget',
  token_budget: 'pages.membersPage.profile_monitor_reason_budget',
  provider_error_budget: 'pages.membersPage.profile_monitor_reason_budget',
  approval_stalled: 'pages.membersPage.profile_monitor_reason_approval',
  approval_stall: 'pages.membersPage.profile_monitor_reason_approval',
  structural_terminal: 'pages.membersPage.profile_monitor_reason_failed',
  model_refused: 'pages.membersPage.profile_monitor_reason_failed',
  interrupted_cycle: 'pages.membersPage.profile_monitor_reason_failed',
  consecutive_failures: 'pages.membersPage.profile_monitor_reason_failed',
  session_start_failures: 'pages.membersPage.profile_monitor_reason_start',
  session_unavailable: 'pages.membersPage.profile_monitor_reason_start',
  sentinel_dropped: 'pages.membersPage.profile_monitor_reason_blocked',
  verdict_stall: 'pages.membersPage.profile_monitor_reason_blocked',
  completion_evidence_unavailable: 'pages.membersPage.profile_monitor_reason_blocked',
  invalid_monitor_record: 'pages.membersPage.profile_monitor_reason_blocked',
  unsupported_monitor_version: 'pages.membersPage.profile_monitor_reason_blocked',
  session_close: 'pages.membersPage.profile_monitor_reason_session_closed',
  monitor_terminal: 'pages.membersPage.profile_monitor_reason_finished',
  stop_sentinel: 'pages.membersPage.profile_monitor_reason_finished',
}

/** Profile reason keys that describe a failure rather than a person or limit. */
const FAILURE_PROFILE_REASON_KEYS = new Set([
  'pages.membersPage.profile_monitor_reason_failed',
  'pages.membersPage.profile_monitor_reason_start',
  'pages.membersPage.profile_monitor_reason_target_unavailable',
  'pages.membersPage.profile_monitor_reason_blocked',
])

const TERMINAL_OUTCOME_REASON_KEYS: Readonly<Record<string, string>> = {
  success: 'pages.membersPage.profile_monitor_reason_finished',
  blocked: 'pages.membersPage.profile_monitor_reason_blocked',
  budget: 'pages.membersPage.profile_monitor_reason_budget',
  user_stop: 'pages.membersPage.profile_monitor_reason_manual',
  session_close: 'pages.membersPage.profile_monitor_reason_session_closed',
  target_unavailable: 'pages.membersPage.profile_monitor_reason_target_unavailable',
}

function stopReason(record: AutomationRecord | null): string {
  if (!record) return ''
  if (record.kind === 'legacy_goal_loop') return record.stoppedReason
  return record.terminal?.reason || (!record.actionable ? record.latest.reasonCode : '')
}

function monitorType(record: AutomationRecord | null, t: ReturnType<typeof useTranslation>['t']): string {
  if (!record) return '—'
  const kind = record.monitorKind
  if (kind === 'work-ledger') return t('pages.membersPage.profile_monitor_subject_ledger')
  if (PULL_REQUEST_WATCH_KINDS.has(kind)) {
    return t('pages.membersPage.profile_monitor_subject_pull_request')
  }
  if (record.kind === 'legacy_goal_loop' && !kind) {
    return t('pages.membersPage.profile_monitor_subject_goal')
  }
  return kind || t('pages.membersPage.profile_monitor_type_bounded')
}

function monitorSubject(record: AutomationRecord | null): string {
  return record?.kind === 'structured_monitor' ? record.target || '—' : '—'
}

function durationValue(seconds: number): string {
  return seconds > 0 ? intervalText(seconds) : '—'
}

export default function CrewMonitorDetails({
  automation,
  readState,
  nowTs,
}: {
  automation: AutomationRecord | null
  readState: MonitorReadState
  nowTs: number
}) {
  const { t } = useTranslation()
  const proven = readState === 'ready'
  const active = proven && isAutomationActive(automation)
  const noMonitor = proven && !automation
  const unsupported = automation?.kind === 'structured_monitor' && !automation.actionable
  const readFailed = readState === 'error'
  const status = readState === 'loading'
    ? t('pages.membersPage.profile_monitor_loading')
    : readFailed || unsupported
      ? t('pages.membersPage.profile_monitor_unavailable')
      : noMonitor
        ? t('pages.membersPage.profile_monitor_none')
        : active
          ? t('pages.membersPage.profile_monitor_active')
          : t('pages.membersPage.profile_monitor_inactive')
  const cadence = automation
    ? durationValue(automation.kind === 'legacy_goal_loop' ? automation.idleSecs : automation.cadenceSecs)
    : '—'
  const currentCycle = automation
    ? fmtNumber(automation.kind === 'legacy_goal_loop' ? automation.cycleCount : automation.usage.probes)
    : '—'
  const maximumCycle = !automation
    ? '—'
    : automation.kind === 'legacy_goal_loop' && automation.maxCycles > 0
      ? fmtNumber(automation.maxCycles)
      : t('pages.membersPage.profile_monitor_no_limit')
  const createdAt = automation?.createdAt ?? 0
  const nextAt = automation?.kind === 'legacy_goal_loop'
    ? automation.nextDueAt ?? 0
    : automation?.nextProbeAt ?? 0
  const maxRuntime = automation?.kind === 'legacy_goal_loop'
    ? automation.maxRuntimeSecs ?? 0
    : automation?.budgets.maxRuntimeSecs ?? 0
  const stoppedAt = automation?.kind === 'structured_monitor'
    ? automation.terminal?.stoppedAt ?? 0
    : 0
  const reason = stopReason(automation)
  const limitEndedRuntime = !active && reason === 'runtime_budget' && maxRuntime > 0
  const runtimeUsed = !automation
    ? '—'
    : limitEndedRuntime
      ? durationValue(maxRuntime)
      : createdAt > 0 && (active || stoppedAt > 0)
        ? durationValue(Math.max(0, (active ? nowTs : stoppedAt) - createdAt))
        : t('pages.membersPage.profile_monitor_unknown')
  const runtime = maxRuntime > 0
    ? t('pages.membersPage.profile_monitor_runtime_value', {
        used: runtimeUsed,
        max: durationValue(maxRuntime),
      })
    : runtimeUsed
  const reasonKey = PROFILE_STOP_REASON_KEYS[reason]
    ?? (automation?.kind === 'structured_monitor'
      ? TERMINAL_OUTCOME_REASON_KEYS[automation.terminal?.outcome ?? '']
      : undefined)
  const reasonText = reasonKey
    ? t(reasonKey)
    : reason
      ? t('pages.membersPage.profile_monitor_reason_unknown')
      : '—'
  // A stop that a failure caused, or a read that failed, is an error and
  // renders through ErrorNotice; ordinary stops stay plain text.
  const reasonFailed = !active
    && reasonKey !== undefined
    && FAILURE_PROFILE_REASON_KEYS.has(reasonKey)

  const rows: Array<[string, string, boolean]> = [
    [t('pages.membersPage.profile_monitor_state'), status, readFailed],
    [t('pages.membersPage.profile_monitor_type'), monitorType(automation, t), false],
    [t('pages.membersPage.profile_monitor_subject'), monitorSubject(automation), false],
    [t('pages.membersPage.profile_monitor_frequency'), cadence, false],
    [t('pages.membersPage.profile_monitor_cycle'), t('pages.membersPage.profile_monitor_cycle_value', { current: currentCycle, max: maximumCycle }), false],
    [t('pages.membersPage.profile_monitor_started'), createdAt > 0 ? fmtDateTime(createdAt) : '—', false],
    [t('pages.membersPage.profile_monitor_next'), active && nextAt > 0 ? fmtDateTime(nextAt) : '—', false],
    [t('pages.membersPage.profile_monitor_runtime'), runtime, false],
    [t('pages.membersPage.profile_monitor_stopped'), active ? '—' : reasonText, reasonFailed],
  ]

  const visibleRows = readFailed || noMonitor || readState === 'loading'
    ? rows.slice(0, 1)
    : rows

  return (
    <section
      className="rounded-2xl border border-border bg-bg px-3.5 py-3"
      data-testid="crew-profile-monitor"
      data-monitor-active={active ? 'true' : 'false'}
    >
      <h3 className="text-[11px] font-semibold tracking-wide uppercase text-muted mb-2">
        {t('pages.membersPage.profile_monitor')}
      </h3>
      <dl className="grid grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] gap-x-3 gap-y-1.5 text-[12px] leading-snug">
        {visibleRows.map(([label, value, failed]) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            {failed ? (
              <dd className="flex min-w-0 justify-end text-right">
                {/* No hand-off: the Members page may hold an unsaved Profile
                    schedule draft or crew-editor draft, and Ask the agent
                    navigates away from it. */}
                <ErrorNotice message={value} variant="inline" testId="crew-profile-monitor-error" />
              </dd>
            ) : (
              <dd className="min-w-0 break-words text-right font-medium text-text" title={value}>{value}</dd>
            )}
          </div>
        ))}
      </dl>
    </section>
  )
}
