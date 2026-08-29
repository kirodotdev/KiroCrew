import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { __resetSessionIdentities, gcOrphanedStorage, noteLiveSessionInstances, recordSessionStorageOwner } from '../utils/storageGc'

/** THE BLOCKING FINDING -- the ledger must be written by the WRITER.
 *
 *  Stamping it from the boot slot list records what the LIST said, not who wrote the
 *  bytes. Session keys are deterministic and REUSED, so a slot recreated under its old
 *  key writes storage as a NEW instance while the ledger still carries the OLD stamp --
 *  and the next boot reads that as supersession and deletes state that is live.
 *
 *  Recording at the write keeps the two in step: the stamp always names the instance
 *  whose bytes are actually on disk. */

const HEIGHTS = 'vc_heights_'
const TABS = 'mc-panel-tabs:'
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

describe('the owner ledger is recorded by the writer', () => {
  beforeEach(() => localStorage.clear())

  /** THE REGRESSION: recreate-under-the-old-key, then boot. */
  it('keeps state a recreated instance wrote under a reused key', () => {
    // Instance i1 runs and writes.
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z')])
    localStorage.setItem(`${HEIGHTS}chat-1`, '{"a":1}')
    recordSessionStorageOwner(`${HEIGHTS}chat-1`)

    // The slot is closed and RECREATED under the same key as instance i2, which writes
    // its own state. The list has moved on; so must the ledger.
    noteLiveSessionInstances([slot('chat-1', '2026-06-06T00:00:00Z')])
    localStorage.setItem(`${HEIGHTS}chat-1`, '{"b":2}')
    recordSessionStorageOwner(`${HEIGHTS}chat-1`)

    // Next boot sees chat-1 live as i2 -- the instance that wrote what is on disk.
    const removed = gcOrphanedStorage([slot('chat-1', '2026-06-06T00:00:00Z')])

    expect(removed).toBe(0)
    expect(localStorage.getItem(`${HEIGHTS}chat-1`)).toBe('{"b":2}')
  })

  /** The collectable case still collects: i1's residue with the list showing i2, and no
   *  i2 write in between, is genuinely superseded. */
  it('still deletes residue the current instance never rewrote', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z')])
    localStorage.setItem(`${HEIGHTS}chat-1`, '{"a":1}')
    localStorage.setItem(`${TABS}chat-1`, '[]')
    recordSessionStorageOwner(`${HEIGHTS}chat-1`)
    recordSessionStorageOwner(`${TABS}chat-1`)

    const removed = gcOrphanedStorage([slot('chat-1', '2026-06-06T00:00:00Z')])

    expect(removed).toBe(2)
    expect(localStorage.getItem(`${HEIGHTS}chat-1`)).toBeNull()
  })

  /** The sweep must never stamp the ledger itself: doing so dates state it did not
   *  write, which is the mechanism the finding names. An unstamped key is uncollectable
   *  -- so a boot that merely OBSERVES an id must not make it collectable next time. */
  it('does not make an unwritten key collectable by observing it', () => {
    localStorage.setItem(`${HEIGHTS}chat-1`, '{"a":1}')

    // Boot once with the id listed as i1 -- no writer ever stamped it.
    expect(gcOrphanedStorage([slot('chat-1', '2026-01-01T00:00:00Z')])).toBe(0)
    // A later boot showing a different instance still has no writer stamp to compare.
    expect(gcOrphanedStorage([slot('chat-1', '2026-06-06T00:00:00Z')])).toBe(0)
    expect(localStorage.getItem(`${HEIGHTS}chat-1`)).toBe('{"a":1}')
  })

  it('records nothing for a key that is not session-scoped', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z')])
    localStorage.setItem('mc-theme', 'dark')
    recordSessionStorageOwner('mc-theme')

    expect(Object.keys(ledgerNow())).toHaveLength(0)
  })

  it('records nothing when the writing instance is unknown', () => {
    localStorage.setItem(`${HEIGHTS}chat-99`, '{}')
    recordSessionStorageOwner(`${HEIGHTS}chat-99`)

    expect(Object.keys(ledgerNow())).toHaveLength(0)
  })

  /** The ledger is bounded by the keys it dates, so a key that is gone drops out. */
  it('drops a ledger entry once the id has no keys left', () => {
    noteLiveSessionInstances([slot('chat-1', '2026-01-01T00:00:00Z')])
    localStorage.setItem(`${HEIGHTS}chat-1`, '{}')
    recordSessionStorageOwner(`${HEIGHTS}chat-1`)
    const ledger = ledgerNow() as unknown as Record<string,
      { stamp?: string }
    >
    expect(ledger[`${HEIGHTS}chat-1`].stamp).toBe('2026-01-01T00:00:00Z')

    localStorage.removeItem(`${HEIGHTS}chat-1`)
    gcOrphanedStorage([slot('chat-1', '2026-01-01T00:00:00Z')])

    expect(Object.keys(ledgerNow())).toHaveLength(0)
  })
})
