import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-1-100'
const live = (created: string) => [{ key: 'chat-1-100', created }]

/**
 * The ledger is deletion PROOF, so a write that fails leaves the PREVIOUS stamp standing as
 * authority over state the current instance owns. The next boot then reads the live instance
 * as newer than the recorded one and deletes a live session's state.
 */
describe('a failed ledger write leaves no deletion proof', () => {
  const realSetItem = Storage.prototype.setItem

  beforeEach(() => {
    window.localStorage.clear()
  })

  afterEach(() => {
    Storage.prototype.setItem = realSetItem
  })

  /** Instance A owns the key, the session is recreated as B, and B's correcting write is the
   *  one that cannot land -- so the only surviving stamp is A's. */
  const leaveStaleProof = (): void => {
    window.localStorage.setItem(KEY, 'a')
    gcOrphanedStorage(live('2026-01-01T00:00:00Z'))
    recordSessionStorageOwner(KEY)

    noteLiveSessionInstances(live('2026-06-06T00:00:00Z'))
    window.localStorage.setItem(KEY, 'b')
    Storage.prototype.setItem = function (): void {
      throw new DOMException('QuotaExceededError')
    }
    recordSessionStorageOwner(KEY)
    Storage.prototype.setItem = realSetItem
  }

  it('drops the ledger rather than keeping the stamp it failed to replace', () => {
    leaveStaleProof()

    expect(window.localStorage.getItem('mc-storage-gc-owner:' + KEY)).toBeNull()
  })

  it('keeps the live session state that stale proof would have doomed', () => {
    leaveStaleProof()

    gcOrphanedStorage(live('2026-06-06T00:00:00Z'))

    expect(window.localStorage.getItem(KEY)).toBe('b')
  })
})
