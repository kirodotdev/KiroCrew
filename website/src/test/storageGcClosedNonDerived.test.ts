import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  MAX_CLOSED_KEYS_PER_SWEEP,
  __resetSessionIdentities,
  gcOrphanedStorage,
  noteLiveSessionInstances,
  noteSessionClosed,
  recordSessionStorageOwner,
} from '../utils/storageGc'

const TABS = 'mc-panel-tabs:'
const slot = (key: string, created: string) => ({ key, created })
const order = (generation: number) => ({ generation, epoch: 'e1' })
const closedMarker = (sessionId: string) =>
  localStorage.getItem(`mc-storage-gc-closed:${sessionId}`)

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('a closed session does not keep its non-derived state forever', () => {
  beforeEach(() => localStorage.clear())

  it('reclaims a uniquely keyed closed session panel state', () => {
    noteLiveSessionInstances([slot('slack:1700000000', '2026-01-01T00:00:00Z')], order(4))
    localStorage.setItem(`${TABS}slack:1700000000`, '{"tab":"files"}')
    recordSessionStorageOwner(`${TABS}slack:1700000000`)
    noteSessionClosed('slack:1700000000')

    gcOrphanedStorage([], order(5))

    expect(localStorage.getItem(`${TABS}slack:1700000000`)).toBeNull()
  })

  it('stops at the per-sweep ceiling and keeps the marker for the next boot', () => {
    const ids = Array.from({ length: MAX_CLOSED_KEYS_PER_SWEEP + 3 }, (_, i) => `slack:9${i}`)
    for (const id of ids) {
      __resetSessionIdentities()
      noteLiveSessionInstances([slot(id, '2026-01-01T00:00:00Z')], order(4))
      localStorage.setItem(`${TABS}${id}`, '{"tab":"files"}')
      recordSessionStorageOwner(`${TABS}${id}`)
      noteSessionClosed(id)
    }

    gcOrphanedStorage([], order(5))

    const left = ids.filter(id => localStorage.getItem(`${TABS}${id}`) !== null)
    expect(left).toHaveLength(3)
    expect(left.every(id => closedMarker(id) !== null)).toBe(true)
  })
})
