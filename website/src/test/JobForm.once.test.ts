import { describe, expect, it } from 'vitest'
import { buildBody, parseJobDefaults } from '../components/JobForm'
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
