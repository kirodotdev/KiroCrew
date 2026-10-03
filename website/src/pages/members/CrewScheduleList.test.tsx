import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import CrewScheduleList from './CrewScheduleList'
import type { CronJob } from '../../types'

/* The profile card's Schedules tab (crewmate-panel IA) is a READABLE list, not
 * the crew editor's pane: one row per schedule that wakes this crewmate, with
 * the two facts a person asks — when it last ran and when it runs next — and a
 * state pill. Nothing here edits; a row opens the Schedule page and the footer
 * pushes the create form. These cases pin the row's facts and its state
 * vocabulary against the job record, in isolation from the page that feeds it.
 */

const NOW = 1_800_000_000 // epoch seconds; `nowTs` is seconds on the page too

function job(overrides: Partial<CronJob> & { id: string; name: string }): CronJob {
  return {
    message: 'go', enabled: true, schedule: '0 9 * * *', last_status: '',
    ...overrides,
  } as CronJob
}

const setup = (jobs: CronJob[], loading = false, error = false) => {
  const onOpenAll = vi.fn()
  const onOpenJob = vi.fn()
  const onCreate = vi.fn()
  render(<CrewScheduleList jobs={jobs} loading={loading} error={error} nowTs={NOW} onOpenAll={onOpenAll} onOpenJob={onOpenJob} onCreate={onCreate} />)
  return { onOpenAll, onOpenJob, onCreate }
}

const rows = () => screen.getAllByTestId('crew-schedule-row')
const rowByName = (name: string) => rows().find((row) => within(row).queryByText(name))!

describe('CrewScheduleList rows', () => {
  it('names each schedule, says when it runs, and carries no edit controls', () => {
    setup([
      job({ id: 'j1', name: 'triage new issues', schedule: '0 9 * * *' }),
      job({ id: 'j2', name: 'weekly digest', schedule: 'every 7d', enabled: false }),
    ])
    const [first, second] = rows()
    expect(first).toHaveTextContent('triage new issues')
    expect(first).toHaveTextContent('0 9 * * *')
    expect(second).toHaveTextContent('weekly digest')
    expect(second).toHaveTextContent('every 7d')
    // Readable, not editable: the editor's row controls (pause / run / open) are
    // not here, and neither is the editor's section.
    expect(screen.queryByTestId('wake-row')).toBeNull()
    expect(screen.queryByTestId('crew-wake-section')).toBeNull()
  })

  it('reads on / paused / running off `enabled` and `is_running`, running first', () => {
    setup([
      job({ id: 'on', name: 'on', enabled: true }),
      job({ id: 'paused', name: 'paused', enabled: false }),
      // Running wins over the enabled flag: the pill says what is happening now.
      job({ id: 'run', name: 'run', enabled: true, is_running: true }),
    ])
    const byName = rowByName
    expect(byName('on')).toHaveAttribute('data-state', 'on')
    expect(within(byName('on')).getByTestId('crew-schedule-state')).toHaveTextContent('Active')
    expect(byName('paused')).toHaveAttribute('data-state', 'paused')
    expect(within(byName('paused')).getByTestId('crew-schedule-state')).toHaveTextContent('Paused')
    expect(byName('run')).toHaveAttribute('data-state', 'running')
    expect(within(byName('run')).getByTestId('crew-schedule-state')).toHaveTextContent('Running')
  })

  it('says a schedule has not run yet rather than inventing a last run', () => {
    setup([job({ id: 'j1', name: 'fresh' })])
    expect(rows()[0]).toHaveTextContent('Has not run yet')
    expect(rows()[0]).not.toHaveTextContent(/Last run/)
  })

  it('reports failed runs inline and once through an actionable notice below the list', () => {
    setup([
      job({ id: 'ok', name: 'ok', last_run_ts: NOW - 3600, last_status: 'ok' }),
      job({ id: 'bad', name: 'bad', last_run_ts: NOW - 3600, last_status: 'error' }),
      job({ id: 'bad-two', name: 'bad-two', last_run_ts: NOW - 7200, last_status: 'failed' }),
      job({ id: 'pending', name: 'pending', last_run_ts: NOW - 3600, last_status: 'pending' }),
    ])
    const byName = rowByName
    expect(byName('ok')).toHaveTextContent(/Last run/)
    expect(byName('ok')).not.toHaveTextContent('failed')
    expect(byName('bad')).toHaveTextContent('failed')
    expect(byName('bad-two')).toHaveTextContent('failed')
    expect(byName('pending')).not.toHaveTextContent('failed')

    const notice = screen.getByTestId('crew-schedule-run-error')
    expect(screen.getAllByTestId('crew-schedule-run-error')).toHaveLength(1)
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent("A schedule's last run failed.")
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(notice.closest('button')).toBeNull()
    const list = rows()[0].closest('ul')!
    expect(list.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows the next run only for an enabled schedule with a future time', () => {
    setup([
      job({ id: 'soon', name: 'soon', next_run_ts: NOW + 90 * 60 }),
      // Paused: a stale next_run_ts must not promise a run that will not happen.
      job({ id: 'paused', name: 'paused', enabled: false, next_run_ts: NOW + 90 * 60 }),
      // Past: the scheduler has not re-armed yet; saying "next in -5m" is wrong.
      job({ id: 'late', name: 'late', next_run_ts: NOW - 300 }),
    ])
    const byName = rowByName
    expect(byName('soon')).toHaveTextContent(/Next in 1h.*30m/)
    expect(byName('paused')).not.toHaveTextContent(/Next in/)
    expect(byName('late')).not.toHaveTextContent(/Next in/)
  })

  it('orders rows by next run and names the soonest enabled one as Up next', () => {
    setup([
      job({ id: 'b', name: 'later', next_run_ts: NOW + 7200 }),
      job({ id: 'a', name: 'sooner', next_run_ts: NOW + 600 }),
      // An earlier time on a PAUSED job is not up next: it will not fire.
      job({ id: 'c', name: 'paused-first', enabled: false, next_run_ts: NOW + 60 }),
      job({ id: 'd', name: 'never', next_run_ts: null }),
    ])
    expect(rows().map((r) => r.textContent).map((text) => ['paused-first', 'sooner', 'later', 'never'].find((name) => text?.includes(name)))).toEqual(['paused-first', 'sooner', 'later', 'never'])
    const next = screen.getByTestId('crew-schedule-next')
    expect(next).toHaveTextContent('Up next')
    expect(next).toHaveTextContent('sooner')
    expect(next).toHaveTextContent('Runs in 10m')
  })

  it('has no Up next card when nothing enabled is due', () => {
    setup([job({ id: 'p', name: 'paused', enabled: false, next_run_ts: NOW + 60 })])
    expect(screen.queryByTestId('crew-schedule-next')).toBeNull()
  })
})

describe('CrewScheduleList states and doors', () => {
  it('a row opens that job; the footer opens the Schedule page and the create form', () => {
    const j = job({ id: 'j1', name: 'triage new issues' })
    const { onOpenAll, onOpenJob, onCreate } = setup([j])
    fireEvent.click(screen.getByRole('button', { name: /triage new issues/ }))
    expect(onOpenJob).toHaveBeenCalledWith('j1')
    expect(onOpenAll).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('crew-schedule-open-all'))
    expect(onOpenAll).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('crew-schedule-create'))
    expect(onCreate).toHaveBeenCalledTimes(1)
  })

  it('says in words that nothing wakes the crewmate, with the create door still offered', () => {
    setup([])
    expect(screen.getByTestId('crew-schedule-empty')).toHaveTextContent(/Nothing wakes this crewmate on its own yet/)
    expect(screen.getByTestId('crew-schedule-create')).toHaveTextContent('New schedule')
    // No `0` anywhere: a count of none is the empty line's job.
    expect(screen.queryByText('0')).toBeNull()
  })

  it('shows a skeleton, not the empty line, while the first read is in flight', () => {
    setup([], true)
    expect(screen.queryByTestId('crew-schedule-empty')).toBeNull()
    expect(screen.queryByTestId('crew-schedule-row')).toBeNull()
    // The create door stays: a list that is still loading can still be added to.
    expect(screen.getByTestId('crew-schedule-create')).toBeInTheDocument()
  })

  it('uses the shared actionable error when the first read fails', () => {
    setup([], false, true)
    const notice = screen.getByTestId('crew-schedule-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(within(notice).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.queryByTestId('crew-schedule-empty')).toBeNull()
  })

  it('keeps showing retained rows below the actionable error after a refetch fails', () => {
    setup([job({ id: 'j1', name: 'triage new issues' })], false, true)
    const notice = screen.getByTestId('crew-schedule-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(rows()).toHaveLength(1)
    expect(rows()[0]).toHaveTextContent('triage new issues')
    expect(notice.compareDocumentPosition(rows()[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('keeps showing the rows it has while a refetch is in flight', () => {
    setup([job({ id: 'j1', name: 'triage new issues' })], true)
    expect(rows()).toHaveLength(1)
  })
})
