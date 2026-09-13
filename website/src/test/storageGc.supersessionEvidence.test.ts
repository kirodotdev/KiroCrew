import { describe, it, expect, beforeEach } from 'vitest'
import { gcOrphanedStorage } from '../utils/storageGc'

const OWNER_ENTRY_PREFIX = 'mc-storage-gc-owner:'

/** THE BLOCKING FINDING -- deleting a session's stored state needs
 *  INSTANCE-LEVEL proof, and neither absence nor age is any.
 *
 *  `localStorage` is shared by every tab on the origin while a slot list is one tab's
 *  snapshot, and SESSION IDS ARE REUSED. So an aged orphan is not a dead one: an id
 *  unlisted past any grace can be resumed under the SAME id by another tab after this
 *  boot's snapshot serialized, and the grace does nothing about it, because the reuse
 *  happens once the grace has already elapsed. Age proves only that the id WAS
 *  unlisted, which is not the proposition deletion needs.
 *
 *  What is proof is SUPERSESSION: the live list dating the id as a DIFFERENT instance
 *  than the one the stored state was written for. */

const HEIGHTS = 'vc_heights_'
const TABS = 'mc-panel-tabs:'

const slot = (key: string, created?: string) => ({ key, created })
/** Seed a stamp for every EXISTING key of each id -- ownership is per KEY, so a per-id
 *  entry would be read as belonging to no key at all. */
const ledger = (entries: Record<string, string>) => {
  for (const [id, stamp] of Object.entries(entries)) {
    const matched: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (!k || k.startsWith(OWNER_ENTRY_PREFIX)) continue
      if (k.endsWith(id) || k.includes(`${id}:`)) matched.push(k)
    }
    for (const k of matched) {
      localStorage.setItem(OWNER_ENTRY_PREFIX + k, JSON.stringify({ stamp, seenAt: 0 }))
    }
  }
}

describe('stored session state is deleted only on proof of supersession', () => {
  beforeEach(() => localStorage.clear())

  it('never deletes an unlisted id, however long it has been unlisted', () => {
    localStorage.setItem(`${HEIGHTS}chat-2`, '{}')
    // An id known since a previous boot and absent from this list -- the exact shape the
    // age gate used to delete on.
    ledger({ 'chat-2': '2026-01-01T00:00:00Z' })

    const removed = gcOrphanedStorage([slot('chat-1', '2026-01-01T00:00:00Z')])

    expect(removed).toBe(0)
    expect(localStorage.getItem(`${HEIGHTS}chat-2`)).toBe('{}')
  })

  it('deletes when the list dates the id as a different instance', () => {
    localStorage.setItem(`${HEIGHTS}chat-2`, '{}')
    localStorage.setItem(`${TABS}chat-2`, '[]')
    ledger({ 'chat-2': '2026-01-01T00:00:00Z' })

    const removed = gcOrphanedStorage([slot('chat-2', '2026-06-06T00:00:00Z')])

    expect(removed).toBe(2)
    expect(localStorage.getItem(`${HEIGHTS}chat-2`)).toBeNull()
  })

  it('keeps the state of the instance that owns it', () => {
    localStorage.setItem(`${HEIGHTS}chat-2`, '{}')
    ledger({ 'chat-2': '2026-01-01T00:00:00Z' })

    expect(gcOrphanedStorage([slot('chat-2', '2026-01-01T00:00:00Z')])).toBe(0)
    expect(localStorage.getItem(`${HEIGHTS}chat-2`)).toBe('{}')
  })

  /** THE RESUME RACE THE FINDING NAMES: this tab's boot snapshot predates another tab's
   *  resume of the same id, so the id is missing from the list while fully live. The
   *  sweep must keep it AND keep its stamp, which is what a later reuse is compared
   *  against -- losing the stamp would silently make the id uncollectable forever. */
  it('survives a boot whose snapshot predates another tab\'s resume', () => {
    localStorage.setItem(`${HEIGHTS}chat-2`, '{}')
    ledger({ 'chat-2': '2026-01-01T00:00:00Z' })

    expect(gcOrphanedStorage([])).toBe(0)
    expect(localStorage.getItem(`${HEIGHTS}chat-2`)).toBe('{}')
    // The stamp is still there, so the next boot can still prove supersession.
    expect(gcOrphanedStorage([slot('chat-2', '2026-06-06T00:00:00Z')])).toBe(1)
  })

  /** The SWEEP must not stamp the ledger. Only the writer may, because a stamp derived
   *  from the list dates bytes the sweep did not write -- which is how a recreated
   *  instance's live state got read as superseded and deleted. */
  it('does not record an instance it merely observed', () => {
    localStorage.setItem(`${HEIGHTS}chat-2`, '{}')

    expect(gcOrphanedStorage([slot('chat-2', '2026-01-01T00:00:00Z')])).toBe(0)
    expect(localStorage.getItem(`mc-storage-gc-owner:${HEIGHTS}chat-2`)).toBeNull()
  })

  /** An undated slot cannot discriminate instances, so it cannot license a delete. */
  it('keeps state when the list carries no instance stamp', () => {
    localStorage.setItem(`${HEIGHTS}chat-2`, '{}')
    ledger({ 'chat-2': '2026-01-01T00:00:00Z' })

    expect(gcOrphanedStorage([slot('chat-2')])).toBe(0)
    expect(localStorage.getItem(`${HEIGHTS}chat-2`)).toBe('{}')
  })
})
