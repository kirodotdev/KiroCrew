import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

/** BLOCKING -- a stale tab must not delete another tab's live state.
 *
 *  Deletion turned on plain inequality, which is satisfied in BOTH directions. A boot GET
 *  issued before a same-key replacement carries the OLDER `created`; by the time that reply
 *  applies, the replacement has already stamped the ledger. `listed !== recorded` held, so the
 *  sweep deleted the REPLACEMENT's panel, activity and preview state -- non-derived, excluded
 *  from DERIVED_PREFIXES, with no rebuild path.
 *
 *  Deletion now needs `listed > recorded` read from parsed instants, and every uncertain
 *  case leaves the key alone. */

const OLD = '2026-01-01T00:00:00Z'
const NEW = '2026-06-06T00:00:00Z'
const KEY = 'mc-panel-tabs:chat-1'
const slot = (key: string, created?: string) => ({ key, created })

/** Stamp KEY as owned by the instance whose `created` is `stamp`. */
const stampedBy = (stamp: string): void => {
  noteLiveSessionInstances([slot('chat-1', stamp)])
  localStorage.setItem(KEY, 'panel state')
  recordSessionStorageOwner(KEY)
}

beforeEach(() => {
  localStorage.clear()
  noteLiveSessionInstances([])
})

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('deletion needs monotonic supersession proof', () => {
  /** THE REGRESSION: the ledger is stamped by the NEWER replacement, and a stale in-flight
   *  list carries the OLDER instance. Inequality holds; supersession does not. */
  it('keeps the replacement state when the listed instance is OLDER', () => {
    stampedBy(NEW)

    gcOrphanedStorage([slot('chat-1', OLD)])

    expect(localStorage.getItem(KEY)).toBe('panel state')
  })

  /** Control: a genuinely newer listing DOES supersede, so the fix withholds deletion for
   *  want of proof rather than abandoning collection. */
  it('deletes when the listed instance is NEWER', () => {
    stampedBy(OLD)

    gcOrphanedStorage([slot('chat-1', NEW)])

    expect(localStorage.getItem(KEY)).toBeNull()
  })

  /** The same instant is not supersession -- one instance, nothing to collect. */
  it('keeps the state when both stamps are the same instant', () => {
    stampedBy(NEW)

    gcOrphanedStorage([slot('chat-1', NEW)])

    expect(localStorage.getItem(KEY)).toBe('panel state')
  })

  /** An unparseable stamp on EITHER side proves nothing, so it fails closed. Both arms are
   *  asserted, because a predicate that only guarded one side would still delete. */
  it('keeps the state when either stamp is unparseable', () => {
    stampedBy('not-a-date')
    gcOrphanedStorage([slot('chat-1', NEW)])
    expect(localStorage.getItem(KEY)).toBe('panel state')

    localStorage.clear()
    noteLiveSessionInstances([])
    stampedBy(NEW)
    gcOrphanedStorage([slot('chat-1', 'not-a-date')])
    expect(localStorage.getItem(KEY)).toBe('panel state')
  })

  /** Stamps at the same instant in DIFFERENT suffix forms must not read as supersession --
   *  the reason ordering is taken from parsed instants and not from string comparison, where
   *  `Z` sorts after `+00:00`. */
  it('keeps the state across equivalent timezone spellings', () => {
    stampedBy('2026-06-06T00:00:00+00:00')

    gcOrphanedStorage([slot('chat-1', '2026-06-06T00:00:00Z')])

    expect(localStorage.getItem(KEY)).toBe('panel state')
  })
})
