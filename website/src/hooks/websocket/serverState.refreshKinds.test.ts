/**
 * A `refresh` frame refetches the caches its kinds name.
 *
 * The frame names what moved (`history`, `crons`, `agents`, ...). A cron
 * kind refetches only its own caches, plus any listed cache whose last read
 * failed; a rare, unknown or missing kind refetches every listed cache.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { invalidateRefreshQueries, TURN_END_CRON_REFRESH_MS, __resetTurnEndCronRefreshForTests } from './serverState'

const KEYS = [
  ['cron-jobs'], ['crons', 'crew-wake', 'ada'], ['cron-history-all'],
  ['agents-installed'], ['kirocrew-agents'], ['kirocrew-agents', 'members-roster'],
  ['default-agent'], ['agent-templates'], ['workspaces'], ['kirocrewConfig'],
  ['resolved-model'], ['agent-resolved-model'], ['artifacts', 'all-tags'], ['artifact-folders'],
]

let qc: QueryClient
let unsubscribe: Array<() => void> = []

afterEach(() => {
  __resetTurnEndCronRefreshForTests()
  vi.useRealTimers()
  for (const u of unsubscribe) u()
  unsubscribe = []
  qc.clear()
})

/** Observe every key with a counting fetcher; `failing` keys reject. */
async function observeAll(failing: string[] = []) {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const fetches: Record<string, number> = {}
  for (const key of KEYS) {
    const id = key.join('/')
    fetches[id] = 0
    const observer = new QueryObserver(qc, {
      queryKey: key,
      queryFn: async () => {
        fetches[id] += 1
        if (failing.includes(id)) throw new Error('offline')
        return { id }
      },
    })
    unsubscribe.push(observer.subscribe(() => {}))
  }
  await settle()
  for (const id of Object.keys(fetches)) fetches[id] = 0
  return fetches
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0))
const refetched = (fetches: Record<string, number>) => Object.keys(fetches).filter(id => fetches[id] > 0).sort()

describe('invalidateRefreshQueries', () => {
  it('refetches the cron caches and the template roster for a cron frame', async () => {
    const fetches = await observeAll()
    invalidateRefreshQueries(qc, ['crons'])
    await settle()
    // The roster's `used_by` names the schedules that reference each template.
    expect(refetched(fetches)).toEqual(['agent-templates', 'cron-history-all', 'cron-jobs', 'crons/crew-wake/ada'])
  })

  it('still refetches a listed cache whose last read failed', async () => {
    const fetches = await observeAll(['artifacts/all-tags', 'kirocrewConfig'])
    invalidateRefreshQueries(qc, ['cron_history'])
    await settle()
    expect(refetched(fetches)).toEqual([
      'artifacts/all-tags', 'cron-history-all', 'cron-jobs', 'crons/crew-wake/ada', 'kirocrewConfig',
    ])
  })

  it.each([
    ['a rare kind', ['agents']],
    ['an unknown kind', ['something-new']],
    ['a narrow kind beside a rare one', ['crons', 'config']],
    ['a cron frame beside a history frame', ['crons', 'history']],
    ['no kinds', []],
    ['an older caller passing nothing', undefined],
  ])('refetches every listed cache for %s', async (_label, kinds) => {
    const fetches = await observeAll()
    invalidateRefreshQueries(qc, kinds)
    await settle()
    expect(refetched(fetches)).toEqual(KEYS.map(k => k.join('/')).sort())
  })

  it.each([
    ['a cron frame', ['crons']],
    ['a rare kind', ['agents']],
    ['no kinds', undefined],
  ])('tells the caller the cron caches moved at once for %s', async (_label, kinds) => {
    await observeAll()
    const stale = vi.fn()
    invalidateRefreshQueries(qc, kinds, stale)
    expect(stale).toHaveBeenCalledTimes(1)
  })
})

describe('a turn-end history frame', () => {
  const CRON_IDS = ['cron-history-all', 'cron-jobs', 'crons/crew-wake/ada']

  it('refetches everything but the cron caches at once', async () => {
    const fetches = await observeAll()
    const stale = vi.fn()
    invalidateRefreshQueries(qc, ['history'], stale)
    await settle()
    expect(refetched(fetches)).toEqual(KEYS.map(k => k.join('/')).filter(id => !CRON_IDS.includes(id)).sort())
    expect(stale).not.toHaveBeenCalled()
  })

  it('re-reads the cron caches once per window however many turns end in it', async () => {
    const fetches = await observeAll()
    vi.useFakeTimers()
    const stale = vi.fn()
    for (let i = 0; i < 5; i++) {
      invalidateRefreshQueries(qc, ['history'], stale)
      vi.advanceTimersByTime(TURN_END_CRON_REFRESH_MS / 10)
    }
    expect(fetches['cron-jobs']).toBe(0)
    vi.advanceTimersByTime(TURN_END_CRON_REFRESH_MS)
    vi.useRealTimers()
    await settle()
    for (const id of CRON_IDS) expect(fetches[id]).toBe(1)
    expect(stale).toHaveBeenCalledTimes(1)
  })

  it('cannot be held off by a steady stream of turns', async () => {
    const fetches = await observeAll()
    vi.useFakeTimers()
    for (let t = 0; t < TURN_END_CRON_REFRESH_MS * 3; t += 1_000) {
      invalidateRefreshQueries(qc, ['history'])
      vi.advanceTimersByTime(1_000)
    }
    vi.useRealTimers()
    await settle()
    expect(fetches['cron-jobs']).toBe(3)
  })

  it('still refetches a cron cache whose last read failed at once', async () => {
    const fetches = await observeAll(['cron-jobs'])
    invalidateRefreshQueries(qc, ['history'])
    await settle()
    expect(fetches['cron-jobs']).toBe(1)
    expect(fetches['cron-history-all']).toBe(0)
  })

  it('does not delay a cron frame that arrives inside the window', async () => {
    const fetches = await observeAll()
    vi.useFakeTimers()
    invalidateRefreshQueries(qc, ['history'])
    invalidateRefreshQueries(qc, ['crons'])
    vi.useRealTimers()
    await settle()
    expect(fetches['cron-jobs']).toBe(1)
  })
})
