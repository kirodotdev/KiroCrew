import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm, { parseJobDefaults, buildBody } from '../components/JobForm'
import type { CronJob } from '../types'

vi.mock('../api/client', () => ({ api: { saveCron: vi.fn(), createCron: vi.fn() } }))

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

  it('initializes tz dropdown from job.timezone', () => {
    renderWithProviders(
      <JobForm job={makeJob({ timezone: 'Africa/Nairobi' })} agents={agents} defaultAgent="gpu-dev" onSaved={() => {}} />,
    )
    const select = screen.getByDisplayValue('Africa/Nairobi')
    expect(select).toBeInTheDocument()
    // Verify non-default TZ is prepended as first option
    const options = Array.from((select as HTMLSelectElement).options)
    expect(options[0].value).toBe('Africa/Nairobi')
  })
})
