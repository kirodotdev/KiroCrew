import { describe, it, expect } from 'vitest'
import { buildBody, parseJobDefaults } from '../components/JobForm'
import type { CronJob } from '../types'

function makeJob(every_secs: number): CronJob {
  return { id: 'test', name: 'test', message: 'test', schedule: '', every_secs, enabled: true } as CronJob
}

const noopError = () => {}

/** What the editor would PUT back for a job whose only edit is an unrelated field. */
function roundTrip(every_secs: number): number | undefined {
  const body = buildBody(parseJobDefaults(makeJob(every_secs)), 'UTC', noopError)
  return body?.every as number | undefined
}

describe('parseJobDefaults interval unit selection', () => {
  it('keeps a 90-minute job in minutes instead of rounding it to 2 hours', () => {
    // 5400 is >= 3600, so the magnitude test chose 'hours' and Math.round(1.5)
    // produced 2. The schedule is exactly representable as 90 minutes.
    const parsed = parseJobDefaults(makeJob(5400))
    expect(parsed.intUnit).toBe('minutes')
    expect(parsed.intVal).toBe(90)
  })

  it('keeps a 36-hour job in hours instead of rounding it to 2 days', () => {
    // The same defect one unit up: 129600 >= 86400 chose 'days', round(1.5) = 2.
    const parsed = parseJobDefaults(makeJob(129600))
    expect(parsed.intUnit).toBe('hours')
    expect(parsed.intVal).toBe(36)
  })

  it.each([
    ['90 minutes', 5400],
    ['36 hours', 129600],
    ['150 minutes', 9000],
    ['1 hour', 3600],
    ['2 hours', 7200],
    ['1 day', 86400],
    ['30 minutes', 1800],
    ['1 week', 604800],
  ])('round-trips %s unchanged through the editor', (_label, secs) => {
    // This is the whole defect: opening a job and saving an unrelated field
    // must not rewrite its schedule. buildBody re-serialises intVal * unit, so
    // any unit the parse could not represent exactly is silently persisted.
    expect(roundTrip(secs)).toBe(secs)
  })

  it('still prefers the largest EXACT unit, not merely the smallest one', () => {
    // A minutes-only rule would round-trip correctly too, but would show a
    // daily job as "1440 minutes". Exactness is necessary, not sufficient.
    expect(parseJobDefaults(makeJob(86400)).intUnit).toBe('days')
    expect(parseJobDefaults(makeJob(7200)).intUnit).toBe('hours')
  })

  it('leaves a sub-minute schedule on the pre-existing nearest-unit behaviour', () => {
    // 90s divides into no unit this form offers. Widening the unit set is a
    // separate question from this defect, so the old choice is deliberately
    // preserved rather than quietly changed under cover of the fix.
    const parsed = parseJobDefaults(makeJob(90))
    expect(parsed.intUnit).toBe('minutes')
    expect(parsed.intVal).toBe(2)
  })

  it('never produces an interval below the input control minimum of 1', () => {
    const parsed = parseJobDefaults(makeJob(10))
    expect(parsed.intVal).toBeGreaterThanOrEqual(1)
  })
})
