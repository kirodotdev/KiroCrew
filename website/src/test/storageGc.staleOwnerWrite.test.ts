import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-7-700'
const OLD = '2026-01-01T00:00:00Z'
const NEW = '2026-06-06T00:00:00Z'
const live = (created: string) => [{ key: 'chat-7-700', created }]

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('a stale cross-tab writer cannot become the recorded owner', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  const recordedStamp = (): string | undefined => {
    const raw = window.localStorage.getItem('mc-storage-gc-owner:' + KEY)
    if (!raw) return undefined
    return (JSON.parse(raw) as { stamp?: string }).stamp
  }

  /** Old tab writes, replacement tab writes, then the old tab's delayed write arrives. */
  const replayTheRace = (): void => {
    window.localStorage.setItem(KEY, 'old-state')
    noteLiveSessionInstances(live(OLD))
    recordSessionStorageOwner(KEY)

    noteLiveSessionInstances(live(NEW))
    window.localStorage.setItem(KEY, 'replacement-state')
    recordSessionStorageOwner(KEY)

    noteLiveSessionInstances(live(OLD))
    recordSessionStorageOwner(KEY)
  }

  it('keeps the newer stamp when the older instance writes last', () => {
    replayTheRace()

    expect(recordedStamp()).toBe(NEW)
  })

  it('leaves the replacement state intact through the next boot sweep', () => {
    replayTheRace()

    gcOrphanedStorage(live(NEW))

    expect(window.localStorage.getItem(KEY)).toBe('replacement-state')
  })
})
