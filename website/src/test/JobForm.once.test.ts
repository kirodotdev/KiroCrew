import { afterEach, describe, expect, it, vi } from 'vitest'
import { buildBody, parseJobDefaults } from '../components/JobForm'
import { defaultFireLocal, fireTimeLocal, toFireTime } from '../components/ScheduleLaterPopover'
import type { CronJob } from '../types'

function oneShot(at_ts: number): CronJob {
  return {
    id: 'one-shot',
    name: 'remind me',
    message: 'ship it',
    schedule: '',
    at_ts,
    enabled: true,
  } as CronJob
}

const noop = () => {}
const FUTURE = Math.floor(Date.now() / 1000) + 86_400

function inZone<T>(tz: string, fn: () => T): T {
  const previous = process.env.TZ
  process.env.TZ = tz
  try {
    return fn()
  } finally {
    process.env.TZ = previous
  }
}

describe('JobForm Run once mode', () => {
  it('detects at_ts instead of falling through to an empty cron expression', () => {
    const parsed = parseJobDefaults(oneShot(FUTURE))
    expect(parsed.schedMode).toBe('once')
    expect(parsed.onceLocal).not.toBe('')
  })

  it('renders and parses the fire time in the same local minute', () => {
    const parsed = parseJobDefaults(oneShot(FUTURE))
    expect(Math.floor(Date.parse(parsed.onceLocal) / 60_000)).toBe(
      Math.floor(FUTURE / 60),
    )
  })

  it('sends one absolute timestamp and no recurring schedule fields', () => {
    const body = buildBody(parseJobDefaults(oneShot(FUTURE)), 'Asia/Tokyo', noop)
    expect(body?.at).toBe(Math.floor(FUTURE / 60) * 60)
    expect(body).not.toHaveProperty('every')
    expect(body).not.toHaveProperty('cron')
    expect(body).not.toHaveProperty('timezone')
  })

  it('rejects a nonexistent DST minute and accepts the adjacent valid time', () => {
    inZone('America/Los_Angeles', () => {
      vi.useFakeTimers()
      vi.setSystemTime(Date.parse('2026-03-08T01:00:00-08:00'))
      try {
        const parsed = parseJobDefaults(oneShot(FUTURE))
        let error = ''

        expect(buildBody(
          { ...parsed, onceLocal: '2026-03-08T02:30' },
          'UTC',
          value => { error = value },
        )).toBeNull()
        expect(error).toBe('Pick a time in the future')

        error = ''
        const body = buildBody(
          { ...parsed, onceLocal: '2026-03-08T03:30' },
          'UTC',
          value => { error = value },
        )
        expect(error).toBe('')
        expect(body?.at).toBe(
          Math.floor(Date.parse('2026-03-08T03:30:00-07:00') / 1000),
        )
      } finally {
        vi.useRealTimers()
      }
    })
  })

  it('does not lose stored seconds on an unrelated edit', () => {
    const parsed = parseJobDefaults(oneShot(FUTURE + 37))
    const body = buildBody({ ...parsed, name: 'renamed' }, 'UTC', noop, true)
    expect(body).not.toHaveProperty('at')
    expect(body?.name).toBe('renamed')
  })

  it('preserves a retained past-due one-shot on an unrelated edit', () => {
    const past = Math.floor(Date.now() / 1000) - 3600
    const parsed = parseJobDefaults(oneShot(past))
    let error = ''

    const body = buildBody(
      { ...parsed, name: 'retained after a denied fire' },
      'UTC',
      value => { error = value },
      true,
    )

    expect(error).toBe('')
    expect(body?.name).toBe('retained after a denied fire')
    expect(body).not.toHaveProperty('at')
  })

  it('sends at when the time itself changes', () => {
    const parsed = parseJobDefaults(oneShot(FUTURE))
    const next = new Date((FUTURE + 3600) * 1000)
    const local = new Date(next.getTime() - next.getTimezoneOffset() * 60_000)
      .toISOString().slice(0, 16)
    const body = buildBody({ ...parsed, onceLocal: local }, 'UTC', noop, true)
    expect(body?.at).toBe(Math.floor((FUTURE + 3600) / 60) * 60)
  })

  it('refuses an empty or past time', () => {
    let error = ''
    const parsed = parseJobDefaults(oneShot(FUTURE))
    expect(buildBody({ ...parsed, onceLocal: '' }, 'UTC', value => { error = value }))
      .toBeNull()
    expect(error).not.toBe('')

    error = ''
    expect(buildBody(
      { ...parsed, onceLocal: '2020-01-01T09:00' },
      'UTC',
      value => { error = value },
    )).toBeNull()
    expect(error).not.toBe('')
  })

  it('ignores a non-finite at_ts rather than rendering an unusable picker', () => {
    expect(parseJobDefaults(oneShot(Number.NaN)).schedMode).not.toBe('once')
  })
})

describe('JobForm converting a stored recurring job to Run once', () => {
  const recurring = (over: Partial<CronJob> = {}): CronJob => ({
    id: 'recurring',
    name: 'standup',
    message: 'post the standup',
    schedule: 'every 1h',
    every_secs: 3600,
    enabled: true,
    ...over,
  } as CronJob)

  afterEach(() => {
    vi.useRealTimers()
  })

  it('seeds the next quarter hour instead of an empty picker', () => {
    vi.useFakeTimers()
    const now = Date.parse('2026-09-17T10:07:30Z')
    vi.setSystemTime(now)

    const parsed = parseJobDefaults(recurring())
    expect(parsed.schedMode).toBe('interval')
    expect(parsed.onceLocal).toBe(defaultFireLocal(now))
    expect(parsed.onceLocal).not.toBe('')
    // Nothing was stored, so there is no "unchanged time" to preserve.
    expect(parsed.onceLocalInitial).toBe('')
  })

  it('saves the conversion with the seeded time as a new absolute at', () => {
    vi.useFakeTimers()
    const now = Date.parse('2026-09-17T10:07:30Z')
    vi.setSystemTime(now)
    let error = ''

    const parsed = parseJobDefaults(recurring({ cron_expr: '0 9 * * 1-5', every_secs: undefined, schedule: '' }))
    expect(parsed.schedMode).toBe('weekly')
    const body = buildBody({ ...parsed, schedMode: 'once' }, 'UTC', value => { error = value }, true)

    expect(error).toBe('')
    expect(body?.at).toBe(toFireTime(defaultFireLocal(now), now))
    expect(body?.at).toBeGreaterThan(Math.floor(now / 1000))
    expect(body).not.toHaveProperty('every')
    expect(body).not.toHaveProperty('cron')
    expect(body).not.toHaveProperty('timezone')
  })

  it('still refuses a conversion whose seeded time has gone past while the form sat open', () => {
    vi.useFakeTimers()
    const mountedAt = Date.parse('2026-09-17T10:07:30Z')
    vi.setSystemTime(mountedAt)
    const parsed = parseJobDefaults(recurring())
    // Negative control for the branch above: the seed is not a free pass.
    vi.setSystemTime(mountedAt + 20 * 60 * 1000)
    let error = ''

    expect(buildBody({ ...parsed, schedMode: 'once' }, 'UTC', value => { error = value }, true)).toBeNull()
    expect(error).toBe('Pick a time in the future')
  })

  it('leaves a stored one-shot alone: its own instant, its own seconds, no re-seed', () => {
    vi.useFakeTimers()
    vi.setSystemTime(Date.parse('2026-09-17T10:07:30Z'))

    const parsed = parseJobDefaults(oneShot(FUTURE + 37))
    expect(parsed.onceLocal).toBe(fireTimeLocal(FUTURE + 37))
    expect(parsed.onceLocalInitial).toBe(parsed.onceLocal)
    expect(parsed.onceLocal).not.toBe(defaultFireLocal())
    expect(buildBody({ ...parsed, name: 'renamed' }, 'UTC', noop, true)).not.toHaveProperty('at')
  })

  it('does not seed a time into the recurring body', () => {
    vi.useFakeTimers()
    vi.setSystemTime(Date.parse('2026-09-17T10:07:30Z'))
    const body = buildBody(parseJobDefaults(recurring()), 'UTC', noop, true)
    expect(body).not.toHaveProperty('at')
    expect(body?.every).toBe(3600)
  })
})


it('refuses a changed past time while editing a one-shot', () => {
  let error = ''
  const parsed = parseJobDefaults(oneShot(FUTURE))

  const body = buildBody(
    { ...parsed, onceLocal: '2020-01-01T09:00' },
    'UTC',
    value => { error = value },
    true,
  )

  expect(body).toBeNull()
  expect(error).not.toBe('')
})
