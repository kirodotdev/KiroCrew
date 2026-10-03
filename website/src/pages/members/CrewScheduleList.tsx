import { AlarmClock, ChevronRight, ExternalLink, Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { CronJob } from '../../types'
import { fmtDateTimeNumeric, fmtDuration } from '../../i18n/format'
import { timeAgo } from '../../utils/timeAgo'
import { cn } from '../../lib/utils'
import ErrorNotice from '../../components/ErrorNotice'

/** Full catalog keys, so the dead-key scan and i18n tooling can see every one. */
const SCHEDULE_STATE_KEYS = {
  on: 'pages.membersPage.schedule_state_on',
  paused: 'pages.membersPage.schedule_state_paused',
  running: 'pages.membersPage.schedule_state_running',
} as const

/** `remaining` seconds as the largest two non-zero units, the goal chip's reading. */
function inDuration(remaining: number): string {
  const d = Math.floor(remaining / 86400)
  const h = Math.floor((remaining % 86400) / 3600)
  const m = Math.floor((remaining % 3600) / 60)
  const parts: Array<[number, 'day' | 'hour' | 'minute']> = d > 0 ? [[d, 'day'], [h, 'hour']] : h > 0 ? [[h, 'hour'], [m, 'minute']] : [[Math.max(m, 1), 'minute']]
  return fmtDuration(parts, { dropZero: true })
}

function lastRunFailed(job: CronJob): boolean {
  return !!job.last_status
    && job.last_status !== 'ok'
    && job.last_status !== 'success'
    && job.last_status !== 'pending'
}

/**
 * The profile card's Schedules tab: one readable row per schedule that wakes
 * this crewmate. Name first; when it runs under it; then the two facts a person
 * actually asks — when it last ran (and how it went) and when it runs next. A
 * state pill on the right says on / paused / running. Nothing here edits: a row
 * opens that job's detail on the Schedule page (`/schedule?job=<id>`), where it
 * is edited, paused or run; the footer opens the create form.
 */
export default function CrewScheduleList({ jobs, loading, error = false, nowTs, onOpenAll, onOpenJob, onCreate }: {
  jobs: CronJob[]
  loading: boolean
  error?: boolean
  nowTs: number
  onOpenAll: () => void
  /** Open one job's detail on the Schedule page. */
  onOpenJob: (id: string) => void
  onCreate: () => void
}) {
  const { t } = useTranslation()
  const sorted = [...jobs].sort((a, b) => (a.next_run_ts ?? Infinity) - (b.next_run_ts ?? Infinity))
  const next = sorted.find((j) => j.enabled && (j.next_run_ts ?? 0) > nowTs)
  const hasFailedRun = sorted.some(lastRunFailed)

  return (
    <div className="flex flex-col gap-3" data-testid="crew-schedule-list">
      {error && (
        <ErrorNotice
          message={t('pages.membersPage.schedule_load_failed')}
          askAgent
          testId="crew-schedule-error"
        />
      )}

      {next && (
        <div className="flex items-center gap-3 rounded-2xl border border-border bg-bg px-3.5 py-3" data-testid="crew-schedule-next">
          <span className="w-10 h-10 rounded-full grid place-items-center bg-accent-subtle text-accent shrink-0" aria-hidden="true"><AlarmClock size={18} /></span>
          <span className="min-w-0 leading-tight">
            <span className="block text-[11px] font-semibold tracking-wide uppercase text-muted">{t('pages.membersPage.schedule_up_next')}</span>
            <span className="block text-[14px] font-semibold truncate">{next.name}</span>
            <span className="block text-[12px] text-muted truncate">{t('pages.membersPage.schedule_in', { time: inDuration(next.next_run_ts! - nowTs) })}</span>
          </span>
        </div>
      )}

      {loading && jobs.length === 0 ? (
        <div className="space-y-2" aria-hidden>
          <div className="h-14 rounded-2xl bg-bg-hover animate-pulse" />
          <div className="h-14 rounded-2xl bg-bg-hover animate-pulse" />
        </div>
      ) : jobs.length === 0 ? (
        !error && (
          <div className="rounded-2xl border border-dashed border-border-strong bg-bg px-4 py-8 text-center text-[13px] text-muted" data-testid="crew-schedule-empty">
            {t('pages.membersPage.schedule_none')}
          </div>
        )
      ) : (
        <ul className="list-none m-0 p-0 rounded-2xl border border-border bg-bg overflow-hidden">
          {sorted.map((j) => {
            const state = j.is_running ? 'running' : j.enabled ? 'on' : 'paused'
            const last = j.last_run_ts ? timeAgo(j.last_run_ts) : null
            const failed = lastRunFailed(j)
            return (
              <li key={j.id} className="border-t border-border first:border-t-0">
                <button
                  type="button"
                  onClick={() => onOpenJob(j.id)}
                  className="w-full flex items-start gap-3 px-3.5 py-3 text-left hover:bg-bg-hover transition-colors cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent"
                  data-testid="crew-schedule-row"
                  data-state={state}
                >
                  <span className="flex-1 min-w-0 leading-tight">
                    <span className="flex items-center gap-2 min-w-0">
                      <span className="text-[13.5px] font-semibold truncate">{j.name}</span>
                      <span
                        className={cn(
                          'ml-auto shrink-0 text-[10.5px] font-semibold px-2 py-0.5 rounded-full',
                          state === 'running' && 'bg-accent-subtle text-accent',
                          state === 'on' && 'bg-ok-subtle text-ok',
                          state === 'paused' && 'bg-bg-hover text-muted',
                        )}
                        data-testid="crew-schedule-state"
                      >
                        {t(SCHEDULE_STATE_KEYS[state])}
                      </span>
                    </span>
                    <span className="block text-[12.5px] text-text mt-0.5 truncate">{j.schedule}</span>
                    <span className="flex flex-wrap gap-x-3 text-[11.5px] text-muted mt-1">
                      <span title={j.last_run_ts ? fmtDateTimeNumeric(j.last_run_ts) : undefined}>
                        {last
                          ? t('pages.membersPage.schedule_last_run', { when: last })
                          : t('pages.membersPage.schedule_never_ran')}
                        {last && failed && <span className="text-danger"> · {t('pages.membersPage.schedule_failed')}</span>}
                      </span>
                      {j.enabled && (j.next_run_ts ?? 0) > nowTs && (
                        <span title={fmtDateTimeNumeric(j.next_run_ts!)}>
                          {t('pages.membersPage.schedule_next_run', { time: inDuration(j.next_run_ts! - nowTs) })}
                        </span>
                      )}
                    </span>
                  </span>
                  <ChevronRight size={16} className="shrink-0 mt-0.5 text-muted" aria-hidden="true" />
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {hasFailedRun && (
        <ErrorNotice
          message={t('pages.membersPage.schedule_run_failed_notice')}
          askAgent
          testId="crew-schedule-run-error"
        />
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onCreate}
          className="inline-flex items-center gap-1.5 h-9 px-4 rounded-full border border-border bg-bg text-[13px] font-semibold hover:bg-bg-hover cursor-pointer"
          data-testid="crew-schedule-create"
        >
          <Plus size={15} aria-hidden="true" />
          {t('pages.membersPage.schedule_new')}
        </button>
        <button
          type="button"
          onClick={onOpenAll}
          className="inline-flex items-center gap-1.5 h-9 px-4 rounded-full border border-border bg-bg text-[13px] font-semibold hover:bg-bg-hover cursor-pointer"
          data-testid="crew-schedule-open-all"
        >
          <ExternalLink size={14} aria-hidden="true" />
          {t('pages.membersPage.schedule_open_all')}
        </button>
      </div>
    </div>
  )
}
