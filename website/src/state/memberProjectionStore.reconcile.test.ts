import { describe, it, expect, beforeEach, vi } from 'vitest'

import { MemberProjectionStore } from './memberProjectionStore'

/**
 * F7: a contributed card whose teardown frame was missed stayed on screen for
 * good. The `value: null` frame is the only thing that removes it, the reconnect
 * baseline deliberately skips contributed keys (their seq is the contributor's
 * own, not the member log's), and a browser that was disconnected when the app
 * was disabled never receives that frame.
 */
describe('MemberProjectionStore.reconcileContributed', () => {
  let store: MemberProjectionStore

  beforeEach(() => {
    store = new MemberProjectionStore()
  })

  it('drops a contributed key the authoritative set no longer holds', () => {
    store.apply('a', 'demoapp/card', { n: 1 }, 5)
    store.reconcileContributed('a', ['other/card'])
    expect(store.get('a', 'demoapp/card')).toBeUndefined()
  })

  it('keeps a contributed key the authoritative set still holds', () => {
    store.apply('a', 'demoapp/card', { n: 1 }, 5)
    store.reconcileContributed('a', ['demoapp/card'])
    expect(store.get('a', 'demoapp/card')).toEqual({ n: 1 })
  })

  it('never touches built-in keys, which the member-log baseline bounds instead', () => {
    store.apply('a', 'roster', { name: 'A' }, 5)
    store.apply('a', 'activity', { n: 2 }, 5)
    store.reconcileContributed('a', [])
    expect(store.get('a', 'roster')).toEqual({ name: 'A' })
    expect(store.get('a', 'activity')).toEqual({ n: 2 })
  })

  it('notifies the dropped key and the contributed keyset, so the card list re-renders', () => {
    store.apply('a', 'demoapp/card', { n: 1 }, 5)
    const onKey = vi.fn()
    const onKeyset = vi.fn()
    store.faceOf('a', 'demoapp/card').subscribe(onKey)
    store.contributedFace('a').subscribe(onKeyset)

    store.reconcileContributed('a', [])

    expect(onKey).toHaveBeenCalled()
    expect(onKeyset).toHaveBeenCalled()
    expect(store.contributedViews('a')).toEqual([])
  })

  it('does not notify when nothing was dropped', () => {
    store.apply('a', 'demoapp/card', { n: 1 }, 5)
    const onKeyset = vi.fn()
    store.contributedFace('a').subscribe(onKeyset)
    store.reconcileContributed('a', ['demoapp/card'])
    expect(onKeyset).not.toHaveBeenCalled()
  })

  it('is a no-op for a slug it holds nothing for', () => {
    expect(() => store.reconcileContributed('nobody', [])).not.toThrow()
    expect(store.has('nobody')).toBe(false)
  })

  it('lets a re-enabled app publish the same key again at its own seq', () => {
    store.apply('a', 'demoapp/card', { n: 1 }, 50)
    store.reconcileContributed('a', [])
    // The row is gone rather than parked at seq 50, so a contributor whose fold
    // restarts at 1 is applied instead of dropped by higher-seq-wins.
    store.apply('a', 'demoapp/card', { n: 'back' }, 1)
    expect(store.get('a', 'demoapp/card')).toEqual({ n: 'back' })
  })

  it('leaves other slugs alone', () => {
    store.apply('a', 'demoapp/card', { n: 1 }, 5)
    store.apply('b', 'demoapp/card', { n: 2 }, 5)
    store.reconcileContributed('a', [])
    expect(store.get('b', 'demoapp/card')).toEqual({ n: 2 })
  })
})
