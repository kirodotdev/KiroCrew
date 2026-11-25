import { describe, it, expect, vi, beforeEach } from 'vitest'

const { api } = vi.hoisted(() => ({ api: { memorySeed: vi.fn() } }))
vi.mock('../api/client', () => ({ api }))
const { SEED_BATCH, seedInBatches } = await import('../pages/overview/memorySeedBatches')

const items = (n: number) => Array.from({ length: n }, (_, i) => ({ kind: 'fact' as const, id: `k${i}` }))
const imported = (batch: { id: string }[]) => ({ results: batch.map(item => ({ id: item.id, outcome: 'imported' as const })) })

beforeEach(() => { vi.clearAllMocks() })

describe('seedInBatches', () => {
  it('sends route-sized batches and reports progress after each', async () => {
    api.memorySeed.mockImplementation(async (_s: string, _t: string, batch: { id: string }[]) => imported(batch))
    const progress = vi.fn()
    const out = await seedInBatches('default', 'member-a', items(120), progress)
    expect(api.memorySeed.mock.calls.map(call => call[2].length)).toEqual([SEED_BATCH, SEED_BATCH, 20])
    expect(progress.mock.calls.map(call => call[0])).toEqual([50, 100, 120])
    expect(out).toEqual({ results: expect.any(Array), partial: false })
    expect(out.results).toHaveLength(120)
  })

  it('stops after a partial batch and marks every later item not attempted', async () => {
    api.memorySeed.mockResolvedValueOnce(imported(items(50))).mockResolvedValueOnce({ partial: true, results: [{ id: 'k50', outcome: 'unconfirmed', reason: 'disk full' }] })
    const out = await seedInBatches('default', 'member-a', items(120))
    expect(api.memorySeed).toHaveBeenCalledTimes(2)
    expect(out.partial).toBe(true)
    expect(out.results.filter(r => r.outcome === 'not_attempted')).toHaveLength(20)
  })

  it('keeps what earlier batches copied when a later request fails, and rejects a first failure', async () => {
    const refused = new Error('A selected memory changed')
    api.memorySeed.mockResolvedValueOnce(imported(items(50))).mockRejectedValueOnce(refused)
    const out = await seedInBatches('default', 'member-a', items(80))
    expect(out.error).toBe(refused)
    expect(out.results.filter(r => r.outcome === 'imported')).toHaveLength(50)
    expect(out.results.filter(r => r.outcome === 'not_attempted')).toHaveLength(30)
    api.memorySeed.mockRejectedValueOnce(refused)
    await expect(seedInBatches('default', 'member-a', items(10))).rejects.toBe(refused)
  })
})
