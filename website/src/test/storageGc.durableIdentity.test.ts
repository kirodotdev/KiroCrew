import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

/** BLOCKING F1 -- a gateway restart must not erase a restored session's
 *  state, and a genuine replacement must still be collected.
 *
 *  Storage ownership keyed on the PROCESS-LOCAL incarnation. A gateway restart rebuilds every
 *  slot object, so every restored session presented a fresh incarnation, the boot sweep read
 *  each one as superseded, and it deleted the live panel, preview, activity and cache state of
 *  sessions the user still had open. Ownership now keys on `created`, which is restored with
 *  the transcript and moves only when a different session takes the key.
 *
 *  This SUPERSEDES the earlier pin that asserted the opposite (collect when only the
 *  incarnation changed): that arrangement IS the restart case, and collecting was the defect.
 *  The two questions need different identities -- the close tombstone still uses the
 *  incarnation, because it must tell a mid-close replacement from the instance it is closing. */

const KEY = 'vc_heights_chat-1'

/** Every arrangement carries BOTH identities, because that is what a real restart looks like:
 *  the transcript's `created` is restored unchanged while the rebuilt slot object gets a fresh
 *  process-local incarnation. A row carrying only `created` would let an incarnation-keyed
 *  sweep collect nothing at all and pass these tests for the wrong reason. */
type Row = { key: string; created: string; incarnation: string }
const slot = (key: string, created: string, incarnation: string): Row =>
  ({ key, created, incarnation })

beforeEach(() => {
  localStorage.clear()
  noteLiveSessionInstances([])
})

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
})

describe('storage ownership uses a restart-durable identity', () => {
  /** THE REGRESSION: one session across a gateway restart -- `created` held, incarnation moved. */
  it('keeps the state of a session restored across a gateway restart', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z', 'inc-before')])
    localStorage.setItem(KEY, 'live height cache')
    recordSessionStorageOwner(KEY)

    gcOrphanedStorage([slot('chat-1', '2026-01-01T00:00:00Z', 'inc-after-restart')])

    expect(localStorage.getItem(KEY)).toBe('live height cache')
  })

  /** Control: a DIFFERENT session on the reused key must still be collected, so the fix is a
   *  change of identity rather than a blanket stop on collecting. */
  it('still collects when a different session takes the key', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z', 'inc-before')])
    localStorage.setItem(KEY, 'previous session cache')
    recordSessionStorageOwner(KEY)

    gcOrphanedStorage([slot('chat-1', '2026-06-06T00:00:00Z', 'inc-replacement')])

    expect(localStorage.getItem(KEY)).toBeNull()
  })

  /** An undated slot licenses no deletion: unstamped is never collectable. */
  it('collects nothing for a slot with no creation stamp', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z', 'inc-before')])
    localStorage.setItem(KEY, 'height cache')
    recordSessionStorageOwner(KEY)

    gcOrphanedStorage([{ key: 'chat-1' }])

    expect(localStorage.getItem(KEY)).toBe('height cache')
  })
})
