import { describe, it, expect } from 'vitest'
import { buildBody, parseJobDefaults } from '../components/JobForm'
import type { CronJob } from '../types'

function makeJob(cron_expr: string): CronJob {
  return { id: 'test', name: 'test', message: 'test', schedule: '', cron_expr, enabled: true } as CronJob
}

const noopError = () => {}

describe('parseJobDefaults multi-time cron expressions (#8469)', () => {
  it('keeps an hour list in cron mode with the raw expression preserved', () => {
    const result = parseJobDefaults(makeJob('0 9,12,15 * * 1-5'))
    expect(result.schedMode).toBe('cron')
    expect(result.cronExpr).toBe('0 9,12,15 * * 1-5')
  })

  it('keeps a minute step in cron mode', () => {
    const result = parseJobDefaults(makeJob('*/15 9 * * 1-5'))
    expect(result.schedMode).toBe('cron')
    expect(result.cronExpr).toBe('*/15 9 * * 1-5')
  })

  it('keeps an hour range in cron mode', () => {
    const result = parseJobDefaults(makeJob('0 9-11 * * 1'))
    expect(result.schedMode).toBe('cron')
    expect(result.cronExpr).toBe('0 9-11 * * 1')
  })

  it('keeps a day-of-week step in cron mode (expandDow cannot represent it)', () => {
    const result = parseJobDefaults(makeJob('0 9 * * 1-5/2'))
    expect(result.schedMode).toBe('cron')
    expect(result.cronExpr).toBe('0 9 * * 1-5/2')
  })

  it('keeps a mixed dow list with an unsupported segment in cron mode', () => {
    // expandDow('1,3-5/2') drops the stepped segment but still returns [1];
    // a whole-field non-empty check would collapse the schedule to Monday.
    const result = parseJobDefaults(makeJob('0 9 * * 1,3-5/2'))
    expect(result.schedMode).toBe('cron')
    expect(result.cronExpr).toBe('0 9 * * 1,3-5/2')
  })

  it('keeps an out-of-range dow token in cron mode instead of wrapping it', () => {
    // parseDowToken applies % 7, which would silently rewrite dow 8 to Monday.
    const result = parseJobDefaults(makeJob('0 9 * * 8'))
    expect(result.schedMode).toBe('cron')
  })

  it('keeps a zero-padded hour in cron mode, matching cronClock grammar', () => {
    const result = parseJobDefaults(makeJob('0 007 * * 1'))
    expect(result.schedMode).toBe('cron')
  })

  it('keeps an out-of-range minute in cron mode', () => {
    const result = parseJobDefaults(makeJob('60 9 * * 1'))
    expect(result.schedMode).toBe('cron')
  })

  it('keeps an out-of-range hour in cron mode instead of fabricating a time', () => {
    const result = parseJobDefaults(makeJob('0 25 * * 1'))
    expect(result.schedMode).toBe('cron')
  })

  it('still parses a named dow range as weekly (regression guard)', () => {
    const result = parseJobDefaults(makeJob('0 13 * * MON-FRI'))
    expect(result.schedMode).toBe('weekly')
    expect(result.weekDays.sort()).toEqual([1, 2, 3, 4, 5])
  })

  it('still parses a plain single-time expression as weekly (regression guard)', () => {
    const result = parseJobDefaults(makeJob('30 9 * * 1,3'))
    expect(result.schedMode).toBe('weekly')
    expect(result.weekTime).toBe('09:30')
    expect(result.weekDays.sort()).toEqual([1, 3])
  })

  it('round-trips a multi-time expression verbatim through buildBody', () => {
    const parsed = parseJobDefaults(makeJob('0 9,12,15 * * 1-5'))
    const body = buildBody(parsed, 'UTC', noopError)
    expect(body).not.toBeNull()
    expect(body!.cron).toBe('0 9,12,15 * * 1-5')
  })
})
