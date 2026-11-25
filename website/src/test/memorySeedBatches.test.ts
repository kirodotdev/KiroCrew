import { describe, it, expect, vi, beforeEach } from 'vitest'

const { api } = vi.hoisted(() => ({ api: { memorySeed: vi.fn(), memberMemoryPage: vi.fn() } }))
vi.mock('../api/client', () => ({ api }))
const { SEED_BATCH, seedInBatches, sourceRows } = await import('../pages/overview/memorySeedBatches')

const items = (n: number) => Array.from({ length: n }, (_, i) => ({ kind: 'fact' as const, id: `k${i}` }))

/** A target store that keeps what it was given, the way seed_item_if_absent does. */
function target() {
  const kept = new Set<string>()
  api.memorySeed.mockImplementation(async (_s: string, _t: string, batch: { id: string }[]) => ({
    results: batch.map(item => {
      if (kept.has(item.id)) return { id: item.id, outcome: 'existing' as const }
      kept.add(item.id)
      return { id: item.id, outcome: 'imported' as const }
    }),
  }))
  return kept
}

/** A source of `facts` semantic rows and `episodes` experiences, served 100 a page. */
function source(facts: number, episodes: number) {
  api.memberMemoryPage.mockImplementation(async (_s: string, kind: string, offset: number) => {
    const total = kind === 'semantic' ? facts : episodes
    const n = Math.max(0, Math.min(100, total - offset))
    return { entries: Array.from({ length: n }, (_, i) => kind === 'semantic' ? { key: `pref.k${offset + i}` } : { id: `ep${offset + i}` }) }
  })
}

beforeEach(() => { vi.clearAllMocks() })

describe('sourceRows', () => {
  it('yields only the picked kinds and never reads experiences when they are not picked', async () => {
    source(150, 30)
    const pages: { kind: string }[][] = []
    for await (const page of sourceRows('default', '', new Set(['fact']))) pages.push(page)
    expect(pages.flat()).toHaveLength(150)
    expect(pages.flat().every(item => item.kind === 'fact')).toBe(true)
    expect(api.memberMemoryPage.mock.calls.every(call => call[1] === 'semantic')).toBe(true)
  })
})

describe('seedInBatches', () => {
  it('copies every matching source row past 1,000, 50 per request, with running counts', async () => {
    const kept = target()
    source(2400, 130)
    const progress = vi.fn()
    const out = await seedInBatches('default', 'member-a', sourceRows('default', 'pref'), progress)
    expect(out.partial).toBe(false)
    expect(out.counts).toEqual({ imported: 2530, existing: 0, failed: 0 })
    expect(kept.size).toBe(2530)
    expect(api.memorySeed.mock.calls.every(call => call[2].length <= SEED_BATCH)).toBe(true)
    expect(api.memberMemoryPage).toHaveBeenCalledWith('default', 'semantic', 2400, 'pref')
    expect(progress.mock.calls.at(-1)![0]).toEqual({ imported: 2530, existing: 0, failed: 0 })
  })

  it('keeps what a failed run copied, and a second run copies the rest with no duplicates', async () => {
    const kept = target()
    const real = api.memorySeed.getMockImplementation()!
    let calls = 0
    api.memorySeed.mockImplementation(async (...args: [string, string, { id: string }[]]) => {
      if (++calls === 7) throw new Error('gateway restarted')
      return real(...args)
    })
    const first = await seedInBatches('default', 'member-a', items(1200))
    expect(first.partial).toBe(true)
    expect(first.error).toBeInstanceOf(Error)
    expect(first.counts.imported).toBe(300)
    api.memorySeed.mockImplementation(real)
    const second = await seedInBatches('default', 'member-a', items(1200))
    expect(second.counts).toEqual({ imported: 900, existing: 300, failed: 0 })
    expect(kept.size).toBe(1200)
  })

  it('stops after a partial batch, and rejects a failure before anything was copied', async () => {
    api.memorySeed.mockResolvedValueOnce({ results: items(50).map(i => ({ id: i.id, outcome: 'imported' })) })
      .mockResolvedValueOnce({ partial: true, results: [{ id: 'k50', outcome: 'unconfirmed', reason: 'disk full' }] })
    const out = await seedInBatches('default', 'member-a', items(120))
    expect(api.memorySeed).toHaveBeenCalledTimes(2)
    expect(out).toMatchObject({ partial: true, counts: { imported: 50, existing: 0, failed: 1 } })
    const refused = new Error('A selected memory changed')
    api.memorySeed.mockRejectedValueOnce(refused)
    await expect(seedInBatches('default', 'member-a', items(10))).rejects.toBe(refused)
  })
})
