/**
 * `addToWatchlist` — the one-line helper over the raw watchlist endpoint.
 *
 * Worth pinning because the whole point of the helper is the SHAPE it wraps the
 * params in: `updateWatchlist` takes a batch envelope (`add` / `update` /
 * `cancel`), so passing the item straight through would be accepted by the
 * types and rejected by the route.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const updateWatchlist = vi.fn()

vi.mock('../api', () => ({
  get updateWatchlist() {
    return updateWatchlist
  },
}))

import { addToWatchlist } from '../mochiHelpers'

beforeEach(() => {
  updateWatchlist.mockReset().mockResolvedValue({ updated: true, items: [] })
})

describe('addToWatchlist', () => {
  it('wraps the item in the batch envelope the endpoint expects', async () => {
    const params = { label: 'zzq-label', kind: 'url' as const, target: 'zzq-target' }
    await addToWatchlist(params)
    expect(updateWatchlist).toHaveBeenCalledTimes(1)
    expect(updateWatchlist).toHaveBeenCalledWith({ add: [params] })
  })

  it('returns the endpoint result untouched', async () => {
    updateWatchlist.mockResolvedValue({ updated: false, items: [{ id: 'zzq-1' }] })
    const res = await addToWatchlist({ label: 'a', kind: 'custom', target: 'b' })
    expect(res).toEqual({ updated: false, items: [{ id: 'zzq-1' }] })
  })
})
