import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm, { parseJobDefaults, buildBody } from '../components/JobForm'
import type { CronJob } from '../types'

vi.mock('../api/client', () => ({ api: { saveCron: vi.fn(), createCron: vi.fn(), updateCron: vi.fn() } }))

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'tz1', name: 'test', message: 'test', schedule: '', enabled: true,
    cron_expr: '0 9 * * 1', ...overrides,
  } as CronJob
}

describe('JobForm timezone initialization', () => {
  it('parseJobDefaults returns weekly schedMode for dow cron', () => {
    const result = parseJobDefaults(makeJob({ timezone: 'UTC' }))
    expect(result.schedMode).toBe('weekly')
  })

  it('parseJobDefaults returns cron schedMode when day-of-month is set', () => {
    const result = parseJobDefaults(makeJob({ cron_expr: '0 9 1-3 * 1-5', timezone: 'UTC' }))
    expect(result.schedMode).toBe('cron')
  })

  it('buildBody sends timezone for weekly mode', () => {
    let error = ''
    const f = {
      name: 'test', message: 'msg', agent: '', channel: '',
      approvalMode: '', silent: false, schedMode: 'weekly' as const,
      intVal: 1, intUnit: 'hours' as const,
      weekDays: [1, 2, 3], weekTime: '09:00', cronExpr: '',
    }
    const body = buildBody(f, 'UTC', e => { error = e })
    expect(error).toBe('')
    expect(body).not.toBeNull()
    expect(body!.timezone).toBe('UTC')
    expect(body!.cron).toBe('0 9 * * 1,2,3')
  })

  it('buildBody sends timezone for cron expression mode', () => {
    let error = ''
    const f = {
      name: 'test', message: 'msg', agent: '', channel: '',
      approvalMode: '', silent: false, schedMode: 'cron' as const,
      intVal: 1, intUnit: 'hours' as const,
      weekDays: [], weekTime: '09:00', cronExpr: '0 9 * * 1-5',
    }
    const body = buildBody(f, 'America/New_York', e => { error = e })
    expect(error).toBe('')
    expect(body).not.toBeNull()
    expect(body!.timezone).toBe('America/New_York')
    expect(body!.cron).toBe('0 9 * * 1-5')
  })

  it('buildBody does not send timezone for interval mode', () => {
    let error = ''
    const f = {
      name: 'test', message: 'msg', agent: '', channel: '',
      approvalMode: '', silent: false, schedMode: 'interval' as const,
      intVal: 2, intUnit: 'hours' as const,
      weekDays: [], weekTime: '09:00', cronExpr: '',
    }
    const body = buildBody(f, 'UTC', e => { error = e })
    expect(error).toBe('')
    expect(body).not.toBeNull()
    expect(body!.timezone).toBeUndefined()
    expect(body!.every).toBe(7200)
  })
})

describe('JobForm timezone render', () => {
  const agents = [{ name: 'gpu-dev', description: '' }]

  /**
   * The zone picker is a `SimpleSelect` (Radix Select) now, so there is no
   * `<select>` to read a display value from and no `.options` to enumerate —
   * the trigger is a button showing the current zone, and the options only
   * exist in the DOM while the popup is open.
   *
   * Located by accessible name, which the site gained with its `aria-label`.
   * Name-based rather than text-based on purpose: an open popup's rows carry
   * the same zone text, so a text query would be ambiguous mid-interaction.
   */
  const tzTrigger = () => screen.getByRole('combobox', { name: 'Timezone' })

  it('initializes tz dropdown from job.timezone', async () => {
    renderWithProviders(
      <JobForm job={makeJob({ timezone: 'Africa/Nairobi' })} agents={agents} defaultAgent="gpu-dev" onSaved={() => {}} />,
    )
    const trigger = tzTrigger()
    expect(trigger).toHaveTextContent('Africa/Nairobi')
    // Verify non-default TZ is prepended as first option
    fireEvent.click(trigger)
    const options = await screen.findAllByRole('option')
    expect(options[0]).toHaveTextContent('Africa/Nairobi')
  })

  it('saves the raw IANA id even though the label hides the underscore', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(
      <JobForm job={makeJob({ timezone: 'Africa/Nairobi' })} agents={agents} defaultAgent="gpu-dev" onSaved={() => {}} />,
    )
    fireEvent.click(tzTrigger())
    // Labels drop the underscore for display; the VALUE behind the row must
    // stay the IANA id. Pins the options/optionLabels pairing — a values-vs-
    // labels mix-up would send "America/New York", which the backend rejects.
    fireEvent.click(await screen.findByRole('option', { name: 'America/New York' }))
    expect(tzTrigger()).toHaveTextContent('America/New York')

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.updateCron).toHaveBeenCalled())
    expect(vi.mocked(api.updateCron).mock.calls[0][1]).toMatchObject({ timezone: 'America/New_York' })
  })
})
