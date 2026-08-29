import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

/** THE BLOCKING FINDING -- ownership is per STORAGE KEY, not per
 *  session id.
 *
 *  A session owns several independent key families (heights, anchors, panel tabs,
 *  activity, web preview), and a replacement instance under a reused id typically writes
 *  only some of them. A per-session stamp advanced by any ONE of those writes then
 *  vouches for every other family, so the families the new instance never touched keep
 *  the OLD instance's state and load straight into the replacement. */

const HEIGHTS = 'vc_heights_'
const TABS = 'mc-panel-tabs:'
const ACTIVITY = 'mc-activity-open:'
const slot = (key: string, created?: string) => ({ key, created })

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

describe('storage ownership is recorded per key', () => {
  beforeEach(() => localStorage.clear())

  /** THE REGRESSION: one family written by the new instance must not save the others. */
  it('still collects the families the replacement instance never rewrote', () => {
    // Instance i1 writes three families.
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z')])
    for (const p of [HEIGHTS, TABS, ACTIVITY]) {
      localStorage.setItem(`${p}chat-1`, '{}')
      recordSessionStorageOwner(`${p}chat-1`)
    }

    // The slot is recreated under the SAME id as i2, which writes only the height cache.
    noteLiveSessionInstances([slot('chat-1', '2026-06-06T00:00:00Z')])
    localStorage.setItem(`${HEIGHTS}chat-1`, '{"new":1}')
    recordSessionStorageOwner(`${HEIGHTS}chat-1`)

    const removed = gcOrphanedStorage([slot('chat-1', '2026-06-06T00:00:00Z')])

    // i1's panel and activity state is stale residue and must go; i2's heights stay.
    expect(removed).toBe(2)
    expect(localStorage.getItem(`${TABS}chat-1`)).toBeNull()
    expect(localStorage.getItem(`${ACTIVITY}chat-1`)).toBeNull()
    expect(localStorage.getItem(`${HEIGHTS}chat-1`)).toBe('{"new":1}')
  })

  it('keeps every family the current instance did rewrite', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z')])
    for (const p of [HEIGHTS, TABS]) {
      localStorage.setItem(`${p}chat-1`, '{}')
      recordSessionStorageOwner(`${p}chat-1`)
    }
    noteLiveSessionInstances([slot('chat-1', '2026-06-06T00:00:00Z')])
    for (const p of [HEIGHTS, TABS]) {
      localStorage.setItem(`${p}chat-1`, '{"new":1}')
      recordSessionStorageOwner(`${p}chat-1`)
    }

    expect(gcOrphanedStorage([slot('chat-1', '2026-06-06T00:00:00Z')])).toBe(0)
    expect(localStorage.getItem(`${TABS}chat-1`)).toBe('{"new":1}')
  })

  it('keys the ledger by full storage key', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z')])
    localStorage.setItem(`${TABS}chat-1`, '[]')
    recordSessionStorageOwner(`${TABS}chat-1`)

    const ledger = ledgerNow() as unknown as Record<string,
      { stamp?: string }
    >
    expect(Object.keys(ledger)).toEqual([`${TABS}chat-1`])
    expect(ledger[`${TABS}chat-1`].stamp).toBe('2026-01-01T00:00:00Z')
  })

  /** Bounded by the keys it dates, so a removed key's entry drops out. */
  it('drops a ledger entry once its key is gone', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z')])
    localStorage.setItem(`${HEIGHTS}chat-1`, '{}')
    recordSessionStorageOwner(`${HEIGHTS}chat-1`)

    localStorage.removeItem(`${HEIGHTS}chat-1`)
    gcOrphanedStorage([slot('chat-1', '2026-01-01T00:00:00Z')])

    expect(Object.keys(ledgerNow())).toHaveLength(0)
  })
})
