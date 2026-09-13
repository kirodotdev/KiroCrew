import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, noteSessionClosed, setSessionScopedItem } from '../utils/storageGc'

const ID = 'chat-12-1200'
const EPOCH = 'gw-epoch-11'
const STAMP = '2026-01-01T00:00:00Z'
const REPLACEMENT_STAMP = '2026-02-02T00:00:00Z'

const NON_DERIVED = [
  `mc-panel-tabs:${ID}`,
  `kirocrew:touched-files:${ID}`,
  `mc-activity-open:${ID}`,
  `mc-busy-send-mode:${ID}`,
]

const own = (generation: number, created = STAMP): void => {
  noteLiveSessionInstances([{ key: ID, created }], { generation, epoch: EPOCH })
  for (const k of NON_DERIVED) setSessionScopedItem(localStorage, k, 'state')
}

const present = (): string[] => NON_DERIVED.filter(k => localStorage.getItem(k) !== null)

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('closure evidence collects only what a resumed tab could rebuild', () => {
  beforeEach(() => localStorage.clear())

  it('keeps non-derived families a resumed tab would share and cannot rebuild', () => {
    own(4)
    noteSessionClosed(ID)

    expect(gcOrphanedStorage([], { generation: 9, epoch: EPOCH })).toBe(0)
    expect(present()).toEqual(NON_DERIVED)
  })

  it('still collects the rebuildable caches of a confirmed-closed session', () => {
    const derived = `vc_heights_${ID}`
    own(4)
    setSessionScopedItem(localStorage, derived, 'measured')
    noteSessionClosed(ID)

    expect(gcOrphanedStorage([], { generation: 9, epoch: EPOCH })).toBe(1)
    expect(localStorage.getItem(derived)).toBeNull()
    expect(present()).toEqual(NON_DERIVED)
  })

  it('keeps a same-key replacement that wrote after the close', () => {
    own(4)
    noteSessionClosed(ID)
    own(7, REPLACEMENT_STAMP)

    expect(gcOrphanedStorage([], { generation: 9, epoch: EPOCH })).toBe(0)
    expect(present()).toEqual(NON_DERIVED)
  })

  it('waits for a frame that actually postdates the close', () => {
    own(9)
    noteSessionClosed(ID)

    expect(gcOrphanedStorage([], { generation: 9, epoch: EPOCH })).toBe(0)
    expect(present()).toEqual(NON_DERIVED)
  })

  it('retains an absent session that was never confirmed closed', () => {
    own(4)

    expect(gcOrphanedStorage([], { generation: 9, epoch: EPOCH })).toBe(0)
    expect(present()).toEqual(NON_DERIVED)
  })

  it('does not act on evidence from another gateway process', () => {
    own(4)
    noteSessionClosed(ID)

    expect(gcOrphanedStorage([], { generation: 9, epoch: 'gw-other' })).toBe(0)
    expect(present()).toEqual(NON_DERIVED)
  })
})
