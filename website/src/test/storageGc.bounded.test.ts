import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { MAX_ABSENT_SESSIONS, __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

/** THE BLOCKING FINDING -- the sweep must BOUND storage.
 *
 *  Requiring positive proof of supersession made every unlisted session's state immortal,
 *  so repeated closes accumulate scoped keys until the origin quota is exhausted -- and
 *  the write that then fails can be a persisted DRAFT, which is a far worse loss than a
 *  re-measured height cache.
 *
 *  Absence still proves nothing, so nothing is deleted for being absent alone. The bound
 *  is a CAPACITY budget: past `MAX_ABSENT_SESSIONS` the COLDEST unlisted sessions go. A
 *  session another tab is really using is being written, so it is never the coldest. */

const HEIGHTS = 'vc_heights_'
const slot = (key: string, created?: string) => ({ key, created })
const sessionKeys = (): string[] => {
  const out: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && !k.startsWith('mc-storage-gc-owner:')) out.push(k)
  }
  return out
}

/** Write `id`'s height cache as its own live instance, at a controlled recency. */
const writeAt = (id: string, at: number): void => {
  noteLiveSessionInstances([slot(id, '2026-01-01T00:00:00Z')])
  localStorage.setItem(`${HEIGHTS}${id}`, '{}')
  const spy = Date.now
  Date.now = () => at
  try {
    recordSessionStorageOwner(`${HEIGHTS}${id}`)
  } finally {
    Date.now = spy
  }
}

const ledgerNow = (): Record<string, unknown> => {
  const out: Record<string, unknown> = {}
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (!k || !k.startsWith('mc-storage-gc-owner:')) continue
    out[k.slice('mc-storage-gc-owner:'.length)] = JSON.parse(localStorage.getItem(k) ?? 'null')
  }
  return out
}

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('the sweep bounds stored session state', () => {
  beforeEach(() => localStorage.clear())

  it('evicts the coldest unlisted sessions once past the budget', () => {
    const over = MAX_ABSENT_SESSIONS + 6
    for (let i = 0; i < over; i++) writeAt(`chat-${i}`, 1_000 + i)

    expect(sessionKeys()).toHaveLength(over)

    // None of them is listed any more.
    const removed = gcOrphanedStorage([])

    expect(removed).toBe(6)
    expect(sessionKeys()).toHaveLength(MAX_ABSENT_SESSIONS)
    // The six coldest went; the newest survived.
    expect(localStorage.getItem(`${HEIGHTS}chat-0`)).toBeNull()
    expect(localStorage.getItem(`${HEIGHTS}chat-5`)).toBeNull()
    expect(localStorage.getItem(`${HEIGHTS}chat-6`)).toBe('{}')
    expect(localStorage.getItem(`${HEIGHTS}chat-${over - 1}`)).toBe('{}')
  })

  it('leaves an unlisted session alone while under the budget', () => {
    writeAt('chat-1', 1_000)

    expect(gcOrphanedStorage([])).toBe(0)
    expect(localStorage.getItem(`${HEIGHTS}chat-1`)).toBe('{}')
  })

  /** The budget must never reach a LISTED session, however cold its last write --
   *  that is the cross-tab hazard the supersession rule exists to prevent. */
  it('never evicts a listed session, even as the coldest of all', () => {
    const over = MAX_ABSENT_SESSIONS + 3
    writeAt('chat-live', 1)
    for (let i = 0; i < over; i++) writeAt(`chat-${i}`, 10_000 + i)

    gcOrphanedStorage([slot('chat-live', '2026-01-01T00:00:00Z')])

    expect(localStorage.getItem(`${HEIGHTS}chat-live`)).toBe('{}')
  })

  it('prunes the ledger entries of the keys it evicted', () => {
    const over = MAX_ABSENT_SESSIONS + 2
    for (let i = 0; i < over; i++) writeAt(`chat-${i}`, 1_000 + i)

    gcOrphanedStorage([])
    const ledger = ledgerNow() as unknown as Record<string, unknown>

    expect(Object.keys(ledger)).toHaveLength(MAX_ABSENT_SESSIONS)
    expect(ledger[`${HEIGHTS}chat-0`]).toBeUndefined()
  })
})
