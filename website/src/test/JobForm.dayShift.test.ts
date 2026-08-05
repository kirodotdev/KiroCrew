import { describe, it, expect } from 'vitest'
import { parseJobDefaults } from '../components/JobForm'
import type { CronJob } from '../types'

function makeJob(cron_expr: string): CronJob {
  return { id: 'test', name: 'test', message: 'test', schedule: '', cron_expr, enabled: true } as CronJob
}

describe('parseJobDefaults weekly cron parsing', () => {
  it('parses hour and minute directly from cron expression', () => {
    const result = parseJobDefaults(makeJob('30 10 * * 1,2,3,4,5'))
    expect(result.weekTime).toBe('10:30')
  })

  it('parses single-digit hour with leading zero', () => {
    const result = parseJobDefaults(makeJob('0 9 * * 1,3,5'))
    expect(result.weekTime).toBe('09:00')
  })

  it('parses days of week without day-shift conversion', () => {
    const result = parseJobDefaults(makeJob('0 12 * * 1,2,3,4,5'))
    expect(result.weekDays.sort()).toEqual([1, 2, 3, 4, 5])
  })

  it('parses weekend days correctly', () => {
    const result = parseJobDefaults(makeJob('0 8 * * 6,0'))
    // Cron dow 6=Sat→grid 6, 0=Sun→grid 7
    expect(result.weekDays.sort()).toEqual([6, 7])
  })

  it('parses evening time correctly', () => {
    const result = parseJobDefaults(makeJob('45 17 * * 4'))
    expect(result.weekTime).toBe('17:45')
    expect(result.weekDays).toEqual([4])
  })

  it('identifies weekly schedule mode', () => {
    const result = parseJobDefaults(makeJob('0 10 * * 1,2,3'))
    expect(result.schedMode).toBe('weekly')
  })

  it('parses named DOW range (MON-FRI)', () => {
    const result = parseJobDefaults(makeJob('0 13 * * MON-FRI'))
    expect(result.schedMode).toBe('weekly')
    expect(result.weekDays.sort()).toEqual([1, 2, 3, 4, 5])
    expect(result.weekTime).toBe('13:00')
  })

  it('parses named DOW comma list (MON,WED,FRI)', () => {
    const result = parseJobDefaults(makeJob('30 9 * * MON,WED,FRI'))
    expect(result.schedMode).toBe('weekly')
    expect(result.weekDays.sort()).toEqual([1, 3, 5])
    expect(result.weekTime).toBe('09:30')
  })
})
