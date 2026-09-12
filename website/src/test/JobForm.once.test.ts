import { describe, it, expect } from 'vitest'
import { buildBody, parseJobDefaults } from '../components/JobForm'
import type { CronJob } from '../types'

/** A one-shot as the API now reports it: a machine-readable `at_ts`, no
 *  recurring spelling. Before the API carried that field, such a job arrived
 *  with only a human-readable `schedule` string. */
function makeOneShot(at_ts: number): CronJob {
  return { id: 'j1', name: 'remind me', message: 'ship it', schedule: '', at_ts, enabled: true } as CronJob
}

function makeCron(cron_expr: string): CronJob {
  return { id: 'j2', name: 'nightly', message: 'x', schedule: '', cron_expr, enabled: true } as CronJob
}

const noopError = () => {}
const FUTURE = Math.floor(Date.now() / 1000) + 86400

describe('parseJobDefaults one-shot detection', () => {
  it('reads a one-shot into once mode instead of an empty cron', () => {
    // The defect this closes: with no at_ts the job matched neither isInterval
    // nor isWeekly, fell through to cron mode with an empty expression, and
    // buildBody then refused to save it — so an MCP-created one-shot could not
    // be edited from this form at all.
    const parsed = parseJobDefaults(makeOneShot(FUTURE))
    expect(parsed.schedMode).toBe('once')
    expect(parsed.onceLocal).not.toBe('')
  })

  it('renders the fire time in the local zone the picker reads back', () => {
    const parsed = parseJobDefaults(makeOneShot(FUTURE))
    // Parsing the input value as local wall clock must land on the same minute.
    // toISOString would have rendered UTC and shifted every viewer outside it.
    expect(Math.floor(Date.parse(parsed.onceLocal) / 1000)).toBe(Math.floor(FUTURE / 60) * 60)
  })

  it('leaves a recurring job on its own mode', () => {
    expect(parseJobDefaults(makeCron('0 9 * * *')).schedMode).toBe('cron')
    expect(parseJobDefaults(makeCron('0 9 * * 1')).schedMode).toBe('weekly')
  })

  it('ignores a non-finite at_ts rather than showing an unusable picker', () => {
    const job = { ...makeOneShot(FUTURE), at_ts: Number.NaN } as CronJob
    expect(parseJobDefaults(job).schedMode).not.toBe('once')
  })
})

describe('buildBody in once mode', () => {
  it('sends an absolute epoch-seconds fire time', () => {
    const f = { ...parseJobDefaults(makeOneShot(FUTURE)) }
    const body = buildBody(f, 'UTC', noopError)
    expect(body?.at).toBe(Math.floor(FUTURE / 60) * 60)
  })

  it('sends no timezone, because an instant has none to choose', () => {
    const body = buildBody(parseJobDefaults(makeOneShot(FUTURE)), 'Asia/Tokyo', noopError)
    expect(body).not.toHaveProperty('timezone')
  })

  it('sends no recurring spelling alongside it', () => {
    const body = buildBody(parseJobDefaults(makeOneShot(FUTURE)), 'UTC', noopError)
    expect(body).not.toHaveProperty('cron')
    expect(body).not.toHaveProperty('every')
  })

  it('refuses an empty picker with a message instead of saving nothing', () => {
    let err = ''
    const f = { ...parseJobDefaults(makeOneShot(FUTURE)), onceLocal: '' }
    expect(buildBody(f, 'UTC', e => { err = e })).toBeNull()
    expect(err).not.toBe('')
  })

  it('refuses a past time, which would fire the moment it is saved', () => {
    let err = ''
    const f = { ...parseJobDefaults(makeOneShot(FUTURE)), onceLocal: '2020-01-01T09:00' }
    expect(buildBody(f, 'UTC', e => { err = e })).toBeNull()
    expect(err).not.toBe('')
  })
})

describe('buildBody omits an unchanged fire time on edit', () => {
  it('sends no `at` when only an unrelated field changed', () => {
    // The picker's step is a minute, so rendering an at_ts that carries seconds
    // truncates it. Re-sending that on a name-only edit moved the run up to 59
    // seconds earlier every time the job was touched.
    const f = parseJobDefaults(makeOneShot(FUTURE + 37))
    const body = buildBody({ ...f, name: 'renamed' }, 'UTC', noopError, true)
    expect(body).not.toHaveProperty('at')
    expect(body?.name).toBe('renamed')
  })

  it('sends `at` when the time control itself changed', () => {
    const f = parseJobDefaults(makeOneShot(FUTURE))
    const moved = { ...f, onceLocal: toLocal(FUTURE + 3600) }
    const body = buildBody(moved, 'UTC', noopError, true)
    expect(body?.at).toBe(Math.floor((FUTURE + 3600) / 60) * 60)
  })

  it('always sends `at` on create, where there is no stored instant', () => {
    const f = parseJobDefaults(makeOneShot(FUTURE))
    expect(buildBody(f, 'UTC', noopError, false)?.at).toBeDefined()
  })
})

/** Render an epoch as the local `datetime-local` text the picker would show. */
function toLocal(epochSecs: number): string {
  const d = new Date(epochSecs * 1000)
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
