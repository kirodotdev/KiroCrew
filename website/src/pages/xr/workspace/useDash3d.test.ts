import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../../api/client'
import { DASH_FIELD } from './dash3d'
import { handleDashboardPageChanged, subscribeDashboardPage } from '../../../hooks/websocket/dashboardFields'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../../../hooks/websocket/serverState'
import { DASH_READ, Paced, dashPageKey, dashPageQuery, layoutOf, pagesToReadAgain, readDash, socketReturned } from './useDash3d'

describe('a whole read of a Bot is paced', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  /** A job whose runs end when the test says so. */
  const job = () => {
    const ends: (() => void)[] = []
    const run = vi.fn(() => new Promise<void>(done => { ends.push(done) }))
    return { run, end: async () => { ends.shift()?.(); await vi.advanceTimersByTimeAsync(0) } }
  }

  it('starts the first ask at once, and keeps one run out at a time', async () => {
    const j = job()
    const gate = new Paced(j.run, 2000)
    gate.ask(); gate.ask(); gate.ask()
    expect(j.run).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10_000)
    // Still out: however long it takes, no second run starts beside it.
    expect(j.run).toHaveBeenCalledTimes(1)
    await j.end()
    expect(j.run).toHaveBeenCalledTimes(2)
  })

  it('starts at most once in two seconds under ten asks a second, and once more after the burst', async () => {
    const j = job()
    const gate = new Paced(j.run, DASH_READ.everyMs)
    const starts: number[] = []
    j.run.mockImplementation(() => { starts.push(Date.now()); return Promise.resolve() })
    const t0 = Date.now()
    for (let i = 0; i < 95; i += 1) { gate.ask(); await vi.advanceTimersByTimeAsync(100) }
    // Nine and a half seconds of asks: a start at 0, 2, 4, 6 and 8 s.
    expect(starts.map(t => t - t0)).toEqual([0, 2000, 4000, 6000, 8000])
    await vi.advanceTimersByTimeAsync(5000)
    // The asks after the last start are answered by one run, two seconds after it, and then nothing more.
    expect(starts.map(t => t - t0)).toEqual([0, 2000, 4000, 6000, 8000, 10_000])
    expect(gate.starts).toBe(6)
  })

  it('goes on after a run that fails, and runs nothing once stopped', async () => {
    const run = vi.fn(() => Promise.reject(new Error('no')))
    const gate = new Paced(run, 2000)
    gate.ask()
    await vi.advanceTimersByTimeAsync(0)
    gate.ask()
    await vi.advanceTimersByTimeAsync(2000)
    expect(run).toHaveBeenCalledTimes(2)
    gate.ask()
    gate.stop()
    await vi.advanceTimersByTimeAsync(10_000)
    gate.ask()
    expect(run).toHaveBeenCalledTimes(2)
  })
})

describe('one whole read', () => {
  it('gives the values', async () => {
    expect(await readDash(async () => ({ dashboard: 'p', read: { fields: { open_prs: 3 }, version: 4 } }))).toEqual({ fields: { open_prs: 3 } })
  })

  it('says that no live page is there when the gateway answers 409, so that one is armed again', async () => {
    expect(await readDash(() => Promise.reject(new ApiError(409, 'no dashboard page is armed for this package')))).toEqual({ gone: true })
  })

  it('is a failed read for any other refusal and for an answer with no values, never values', async () => {
    expect(await readDash(() => Promise.reject(new ApiError(500, 'no')))).toEqual({ failed: true })
    expect(await readDash(() => Promise.reject(new TypeError('network')))).toEqual({ failed: true })
    for (const got of [null, {}, { read: null }, { read: [] }, { read: { fields: [] } }, { read: { fields: 'x' } }]) {
      expect(await readDash(async () => got)).toEqual({ failed: true })
    }
  })
})

describe('the layout that a Bot\'s values hold', () => {
  it('is none when the field is not there or was emptied', () => {
    expect(layoutOf(null)).toBeNull()
    expect(layoutOf({ open_prs: 3 })).toBeNull()
    expect(layoutOf({ [DASH_FIELD]: null })).toBeNull()
    // A clear writes an empty record: the dashboard is gone, not broken.
    expect(layoutOf({ [DASH_FIELD]: {} })).toBeNull()
    expect(layoutOf(Object.create({ [DASH_FIELD]: { v: 1, parts: [] } }) as Record<string, unknown>)).toBeNull()
  })

  it('is a layout, or one that says why it is not drawn', () => {
    expect(layoutOf({ [DASH_FIELD]: { v: 1, parts: [{ kind: 'number', name: 'Open PRs', field: 'open_prs' }] } })?.parts).toHaveLength(1)
    expect(layoutOf({ [DASH_FIELD]: { v: 3 } })).toEqual({ parts: [], why: 'version' })
    expect(layoutOf({ [DASH_FIELD]: 'ring' })).toEqual({ parts: [], why: 'not_layout' })
  })
})

describe('the room\'s read of a Bot\'s page', () => {
  it('is kept under a key that the dashboard\'s own frames do not refresh, and is never stale by itself', () => {
    const q = dashPageQuery('mira', 'Mira')
    expect(q.queryKey[0]).not.toBe(MEMBER_DASHBOARD_QUERY_PREFIX)
    expect(q.queryKey).toEqual(dashPageKey('mira'))
    expect(q.staleTime).toBe(Infinity)
    expect([q.refetchOnWindowFocus, q.refetchOnReconnect, q.refetchOnMount, q.retry]).toEqual([false, false, false, false])
  })

  it('is told of a changed page by its own frame, by slug, and of nothing else', () => {
    const told: string[] = []
    const stop = subscribeDashboardPage(slug => told.push(slug))
    handleDashboardPageChanged({ slug: 'mira' })
    for (const bad of [null, {}, { slug: '' }, { slug: 7 }, { slot: 'x' }, 'mira']) handleDashboardPageChanged(bad)
    expect(told).toEqual(['mira'])
    stop()
    handleDashboardPageChanged({ slug: 'mira' })
    expect(told).toEqual(['mira'])
  })
})

describe('whose page is read again with no frame asking for it', () => {
  const all = ['ada', 'bo', 'cy']
  it('is every Bot when the socket comes back: a first apply while it was down had no frame that was seen', () => {
    expect(pagesToReadAgain('reconnect', all, ['bo'])).toEqual(['ada', 'bo', 'cy'])
    expect(pagesToReadAgain('reconnect', all, [])).toEqual(['ada', 'bo', 'cy'])
  })
  it('is the Bots with a package each minute, and the Bots with none each ten minutes', () => {
    expect(pagesToReadAgain('clock', all, ['bo'])).toEqual(['bo'])
    expect(pagesToReadAgain('slow', all, ['bo'])).toEqual(['ada', 'cy'])
    expect(DASH_READ.slowMs).toBe(600_000)
    expect(DASH_READ.slowMs).toBeGreaterThan(DASH_READ.fallbackMs)
  })
})

describe('the socket coming back', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('is not its first connect after the page loads: the pages are being read then anyway', () => {
    expect(socketReturned(false, true, false)).toBe(false)
    expect(socketReturned(false, true, true)).toBe(true)
    // Up and still up, down, and going down are no return either.
    expect(socketReturned(true, true, true)).toBe(false)
    expect(socketReturned(false, false, true)).toBe(false)
    expect(socketReturned(true, false, true)).toBe(false)
  })

  it('starts a round of reads at most every 30 s while the socket goes up and down every 10 s', async () => {
    const starts: number[] = []
    const t0 = Date.now()
    const gate = new Paced(() => { starts.push(Date.now() - t0); return Promise.resolve() }, DASH_READ.reconnectMs)
    for (let i = 0; i < 6; i += 1) { gate.ask(); await vi.advanceTimersByTimeAsync(10_000) }
    // Returns at 0, 10, ... 50 s: rounds at 0 and 30 s, one more at 60 s for the returns after, and none after.
    expect(DASH_READ.reconnectMs).toBeGreaterThanOrEqual(30_000)
    expect(starts).toEqual([0, 30_000, 60_000])
    await vi.advanceTimersByTimeAsync(120_000)
    expect(starts).toHaveLength(3)
    gate.stop()
  })
})
