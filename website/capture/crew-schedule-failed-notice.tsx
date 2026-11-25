/**
 * Capture entry: a Bot profile's Schedules list with the failed-run notice.
 * Mounts the REAL CrewScheduleList with mocked jobs:
 *
 * - ?scenario=auto     a job Kiro Crew paused after 5 failures, plus one the
 *                      user paused after a failure (no notice for that one).
 * - ?scenario=user     only the user-paused failure: no notice.
 * - ?scenario=many     two active jobs whose last run failed.
 * ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n/all'
import CrewScheduleList from '../src/pages/members/CrewScheduleList'
import type { CronJob } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scenario = params.get('scenario') || 'auto'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const NOW = Math.floor(Date.now() / 1000)
const base = { message: 'go', last_status: 'ok', enabled: true } as const
const job = (o: Partial<CronJob> & { id: string; name: string; schedule: string }): CronJob => ({ ...base, ...o }) as CronJob

const ok = job({ id: 'a', name: 'Weekday thread ledger', schedule: 'every 30m', last_run_ts: NOW - 1200, next_run_ts: NOW + 600 })
const autoPaused = job({ id: 'b', name: 'Thread ledger to board', schedule: 'every 10m', enabled: false, user_paused: false, last_status: 'error', consecutive_failures: 5, last_run_ts: NOW - 86400 })
const userPaused = job({ id: 'c', name: 'Release board patrol', schedule: 'every 20m', enabled: false, user_paused: true, last_status: 'error', consecutive_failures: 1, last_run_ts: NOW - 9 * 86400 })
const activeBad = job({ id: 'd', name: 'Nightly digest', schedule: '0 2 * * *', last_status: 'error', consecutive_failures: 1, last_run_ts: NOW - 7200, next_run_ts: NOW + 3600 * 20 })
const activeBad2 = job({ id: 'e', name: 'Issue triage', schedule: 'every 1h', last_status: 'error', consecutive_failures: 2, last_run_ts: NOW - 1800, next_run_ts: NOW + 1800 })

const jobs = scenario === 'user' ? [ok, userPaused] : scenario === 'many' ? [ok, activeBad, activeBad2] : [ok, userPaused, autoPaused]

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <div className="min-h-screen bg-bg-elevated text-text p-4" data-capture-root>
    <div className="max-w-[420px]">
      <CrewScheduleList
        jobs={jobs}
        loading={false}
        nowTs={NOW}
        onOpenAll={() => {}}
        onOpenJob={() => {}}
        onCreate={() => {}}
      />
    </div>
  </div>,
)
