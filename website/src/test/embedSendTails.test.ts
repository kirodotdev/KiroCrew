/**
 * `retireSendTail`: ChatEmbed's per-send tail records, retired one at a time.
 *
 * Pinned:
 * - retiring a record removes THAT record only; others keep their ids.
 * - the retired record's text is taken back only when the composer still
 *   holds exactly what the restore chain produced; an edit since leaves it.
 * - retiring the FIRST of two restores rebuilds the composer around the
 *   second's text and re-bases the second's before/after, so retiring it next
 *   empties the composer.
 * - a restore that started from something other than the previous restore's
 *   result (the user typed in between) breaks the chain: notice retires, text
 *   stays.
 * - a record that restored nothing (a chip send) retires without touching
 *   the composer; an unknown id changes nothing.
 */
import { describe, it, expect } from 'vitest'
import { retireSendTail, type SendTail } from '../app-sdk/embedSendTails'
import { mergeRecoveredDraft } from '../utils/chatDrafts'

const notice = (sendId: string, restore?: SendTail['restore']): SendTail =>
  ({ role: 'notice', content: 'Delivery not confirmed', sendId, restore })

describe('retireSendTail', () => {
  it('retires only the named record and gives back its text when the composer is untouched', () => {
    const a = notice('a', { text: 'hello', before: '', after: 'hello' })
    const r = retireSendTail([a], 'a', 'hello', mergeRecoveredDraft)
    expect(r.tails).toEqual([])
    expect(r.draft).toBe('')
  })

  it('leaves an edited composer alone', () => {
    const a = notice('a', { text: 'hello', before: '', after: 'hello' })
    const r = retireSendTail([a], 'a', 'hello, and more', mergeRecoveredDraft)
    expect(r.tails).toEqual([])
    expect(r.draft).toBeNull()
  })

  it('retiring the first of two chained restores rebuilds the composer around the second', () => {
    const a = notice('a', { text: 'first', before: '', after: 'first' })
    const b = notice('b', { text: 'second', before: 'first', after: 'first\n\nsecond' })
    const r = retireSendTail([a, b], 'a', 'first\n\nsecond', mergeRecoveredDraft)
    expect(r.draft).toBe('second')
    expect(r.tails).toHaveLength(1)
    expect(r.tails[0].sendId).toBe('b')
    expect(r.tails[0].restore).toEqual({ text: 'second', before: '', after: 'second' })
    // Then retiring B from the rebuilt state empties the composer.
    const r2 = retireSendTail(r.tails, 'b', r.draft!, mergeRecoveredDraft)
    expect(r2.tails).toEqual([])
    expect(r2.draft).toBe('')
  })

  it('retiring the second of two chained restores takes back only its own text', () => {
    const a = notice('a', { text: 'first', before: '', after: 'first' })
    const b = notice('b', { text: 'second', before: 'first', after: 'first\n\nsecond' })
    const r = retireSendTail([a, b], 'b', 'first\n\nsecond', mergeRecoveredDraft)
    expect(r.draft).toBe('first')
    expect(r.tails.map(t => t.sendId)).toEqual(['a'])
    expect(r.tails[0].restore).toEqual({ text: 'first', before: '', after: 'first' })
  })

  it('text typed BETWEEN two restores breaks the chain: the notice retires, the text stays', () => {
    const a = notice('a', { text: 'first', before: '', after: 'first' })
    // B's restore started from "first typed", not from A's result.
    const b = notice('b', { text: 'second', before: 'first typed', after: 'first typed\n\nsecond' })
    const r = retireSendTail([a, b], 'a', 'first typed\n\nsecond', mergeRecoveredDraft)
    expect(r.tails.map(t => t.sendId)).toEqual(['b'])
    expect(r.draft).toBeNull()
  })

  it("replays later restores with the CALLER's merge, so a single-line composer never gets the paragraph break back", () => {
    // ChatEmbed merges with the shared rule flattened to a space; the replay
    // must use that same function, or retiring A would write "first\n\nsecond"
    // -- the raw rule's join -- into an <input> that cannot hold a newline
    // (the GPT finding this pins).
    const flat = (before: string, text: string) => (before ? `${before} ${text}` : text)
    const a = notice('a', { text: 'first', before: '', after: 'first' })
    const b = notice('b', { text: 'second', before: 'first', after: 'first second' })
    const c = notice('c', { text: 'third', before: 'first second', after: 'first second third' })
    const r = retireSendTail([a, b, c], 'a', 'first second third', flat)
    expect(r.draft).toBe('second third')
    expect(r.draft).not.toContain('\n')
    expect(r.tails.map(t => t.restore)).toEqual([
      { text: 'second', before: '', after: 'second' },
      { text: 'third', before: 'second', after: 'second third' },
    ])
  })

  it('a record that restored nothing retires without touching the composer', () => {
    const chip = notice('c')
    const a = notice('a', { text: 'hello', before: '', after: 'hello' })
    const r = retireSendTail([chip, a], 'c', 'hello', mergeRecoveredDraft)
    expect(r.tails.map(t => t.sendId)).toEqual(['a'])
    expect(r.draft).toBeNull()
  })

  it('an unknown id changes nothing', () => {
    const a = notice('a', { text: 'hello', before: '', after: 'hello' })
    const r = retireSendTail([a], 'zzz', 'hello', mergeRecoveredDraft)
    expect(r.tails).toEqual([a])
    expect(r.draft).toBeNull()
  })
})
