/**
 * A mode switch REPLACES a slot; it does not close one.
 *
 * The toggle creates a replacement slot and retires the old one. The buckets are keyed by
 * slot, so deleting the old key's entries destroyed the only copy of unsent work — the
 * composer remounts against the new key and no store answers for the old one.
 *
 * The copy is deliberately NOT a move, for two reasons this suite pins: the replacement
 * activates before the deletion is awaited, so the entry must already be under the new key
 * when the slot-change effect restores the composer; and a FAILED deletion leaves the old
 * slot alive, still needing its own work.
 */
import { describe, expect, it } from 'vitest'

import { copySlotEntry } from '../utils/draftMigration'

const OLD = 'chat-old-1'
const NEW = 'chat-new-2'

describe('unsent work follows a slot that is being replaced', () => {
  it('seeds the replacement key', () => {
    const bucket: Record<string, string> = { [OLD]: 'the text I have not sent' }

    expect(copySlotEntry(bucket, OLD, NEW)).toBe(true)
    expect(bucket[NEW]).toBe('the text I have not sent')
  })

  it('RETAINS the original, so a failed deletion loses nothing', () => {
    // The discriminating case against a move: the caller drops the old entry only after the
    // deletion succeeds, because a failure leaves that slot alive and still holding its work.
    const bucket: Record<string, string> = { [OLD]: 'text' }

    copySlotEntry(bucket, OLD, NEW)
    expect(bucket[OLD]).toBe('text')
  })

  it('carries a non-string composition part unchanged', () => {
    // Staged files, pasted blocks and session refs are parts of the same unsent message, so
    // they travel by the same rule rather than by four hand-written copies.
    const tokens = ['tok-1', 'tok-2']
    const bucket: Record<string, string[]> = { [OLD]: tokens }

    copySlotEntry(bucket, OLD, NEW)
    expect(bucket[NEW]).toBe(tokens)
  })

  it('does NOT invent an entry for a bucket the slot never had', () => {
    // Writing `undefined` under the new key leaves an empty-but-present draft, which the
    // close guard reads as unsent work — the replacement slot would refuse its own close.
    const bucket: Record<string, string> = { 'chat-other': 'theirs' }

    expect(copySlotEntry(bucket, OLD, NEW)).toBe(false)
    expect(NEW in bucket).toBe(false)
    expect(bucket['chat-other']).toBe('theirs')
  })

  it('is a no-op when the replacement key equals the retired one', () => {
    const bucket: Record<string, string> = { [OLD]: 'text' }

    expect(copySlotEntry(bucket, OLD, OLD)).toBe(false)
    expect(bucket[OLD]).toBe('text')
  })

  it('refuses an empty key rather than creating an entry under it', () => {
    const bucket: Record<string, string> = { [OLD]: 'text' }

    expect(copySlotEntry(bucket, OLD, '')).toBe(false)
    expect('' in bucket).toBe(false)
  })

  it('leaves every other slot untouched', () => {
    const bucket: Record<string, string> = { [OLD]: 'mine', 'chat-other': 'theirs' }

    copySlotEntry(bucket, OLD, NEW)
    expect(bucket['chat-other']).toBe('theirs')
    expect(bucket[NEW]).toBe('mine')
  })

  it('preserves a falsy-but-present entry, which absence would discard', () => {
    // An empty string is a real stored state (the user cleared the box), and telling it apart
    // from absence is what `in` does and a truthiness check does not.
    const bucket: Record<string, string> = { [OLD]: '' }

    expect(copySlotEntry(bucket, OLD, NEW)).toBe(true)
    expect(bucket[NEW]).toBe('')
  })
})
