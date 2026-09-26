import { describe, it, expect } from 'vitest'
import { buildRecallEntries, type RecallAttempt, type RecallMessage } from './recallHistory'

/** Texts only. A local shim, because production has no text-only caller and an
 *  exported wrapper with none is surface this PR should not add. */
const buildRecallHistory = (m: readonly RecallMessage[], a?: readonly RecallAttempt[]) =>
  buildRecallEntries(m, a).map((e) => e.text)
import type { PasteBlock } from '../utils/pasteTokens'

const user = (content: string, sendId?: string): RecallMessage =>
  ({ role: 'user', content, ...(sendId ? { meta: { sendId } } : {}) })
const assistant = (content: string): RecallMessage => ({ role: 'assistant', content })
const attempt = (text: string, sendId = 's-lost'): RecallAttempt => ({ text, sendId })

/** The wire form of `WIRE_COMPOSER`: expanded markers the server persists. */
const WIRE_COMPOSER = 'look at @notes.txt'
const WIRE_PERSISTED = 'look at [attached_file 1] /home/u/notes.txt'

describe('buildRecallHistory', () => {
  it('returns transcript prompts oldest to newest, ignoring non-user rows', () => {
    expect(buildRecallHistory([user('one'), assistant('reply'), user('two')])).toEqual(['one', 'two'])
  })

  it('prefers rawText over content when both are present', () => {
    expect(buildRecallHistory([{ role: 'user', content: 'rendered', rawText: 'typed' }])).toEqual(['typed'])
  })

  it('collapses consecutive duplicates but keeps a non-consecutive repeat', () => {
    expect(buildRecallHistory([user('a'), user('a'), user('b'), user('a')])).toEqual(['a', 'b', 'a'])
  })

  it('skips empty rows', () => {
    expect(buildRecallHistory([user(''), user('real')])).toEqual(['real'])
  })

  // The defect this module exists for: without the merge these return only the
  // transcript, so the prompt the user watched vanish is unreachable by ↑.
  it('appends a submitted prompt the transcript never took', () => {
    expect(buildRecallHistory([user('landed', 's-1')], [attempt('lost')])).toEqual(['landed', 'lost'])
  })

  it('offers the lost prompt on the FIRST up press, i.e. at the tail', () => {
    const out = buildRecallHistory([user('older', 's-1'), user('landed', 's-2')], [attempt('lost')])
    expect(out[out.length - 1]).toBe('lost')
  })

  it('recovers the prompt when the transcript is empty', () => {
    expect(buildRecallHistory([], [attempt('lost')])).toEqual(['lost'])
  })

  it('ignores an empty submitted prompt', () => {
    expect(buildRecallHistory([user('landed')], [attempt('')])).toEqual(['landed'])
  })

  it('behaves identically with no attempts and with an empty attempts list', () => {
    const msgs = [user('one'), user('two')]
    expect(buildRecallHistory(msgs)).toEqual(buildRecallHistory(msgs, []))
  })
})

/**
 * Whether a prompt is already on screen is decided by the send's id.
 *
 * Text cannot decide it — the server persists a wire form the composer never
 * held — and a timestamp cannot either, because the row and the submission are
 * stamped by different machines. The id is one value on one clock, and the
 * backend returns it on the row, so it answers the question outright.
 */
describe('buildRecallHistory matches an attempt to its row by send id', () => {
  it('does not offer an attempt whose id is already on a transcript row', () => {
    expect(buildRecallHistory([user('same', 's-7')], [attempt('same', 's-7')])).toEqual(['same'])
  })

  it('offers an attempt whose id is on no transcript row', () => {
    expect(buildRecallHistory([user('landed', 's-7')], [attempt('lost', 's-8')]))
      .toEqual(['landed', 'lost'])
  })

  it('recognises its row even though the server persisted a different spelling', () => {
    const out = buildRecallHistory([user(WIRE_PERSISTED, 's-9')], [attempt(WIRE_COMPOSER, 's-9')])
    expect(out).toEqual([WIRE_PERSISTED])
  })

  it('offers a lost send whose text a landed row happens to share', () => {
    const msgs = [user('repeat', 's-1'), user('other', 's-2')]
    expect(buildRecallHistory(msgs, [attempt('repeat', 's-3')]))
      .toEqual(['repeat', 'other', 'repeat'])
  })

  it('treats a row with no id as no evidence about any attempt', () => {
    expect(buildRecallHistory([user('legacy row')], [attempt('lost', 's-4')]))
      .toEqual(['legacy row', 'lost'])
  })

  it('offers an attempt carrying no id, rather than silently dropping it', () => {
    expect(buildRecallHistory([user('landed', 's-1')], [{ text: 'lost', sendId: '' }]))
      .toEqual(['landed', 'lost'])
  })

  it('counts an id on any user row, including a still-optimistic bubble', () => {
    const optimistic: RecallMessage = { role: 'user', content: 'in flight', meta: { sendId: 's-5' } }
    expect(buildRecallHistory([user('older', 's-4'), optimistic], [attempt('in flight', 's-5')]))
      .toEqual(['older', 'in flight'])
  })

  it('ignores a non-string id on a row', () => {
    const odd: RecallMessage = { role: 'user', content: 'weird', meta: { sendId: 42 } }
    expect(buildRecallHistory([odd], [attempt('lost', 's-6')])).toEqual(['weird', 'lost'])
  })

  it('offers several distinct lost prompts, newest at the tail', () => {
    const out = buildRecallHistory(
      [user('landed', 's-1')],
      [attempt('first lost', 's-2'), attempt('second lost', 's-3')],
    )
    expect(out).toEqual(['landed', 'first lost', 'second lost'])
    expect(out[out.length - 1]).toBe('second lost')
  })

  /** Pins the module doc's stated NON-GOAL. The window here holds only the newest
   *  row, so the older landed prompt's id is absent and its record — never
   *  retired, because no fetched page carried that row — is offered again at the
   *  tail. Reading that absence as "never landed" is the failure this trades
   *  against: a wholesale refresh empties the same rows, and there it would
   *  discard the prompt outright instead of offering one twice. */
  it('re-offers a landed prompt whose row is outside the passed-in window', () => {
    const loadedWindow = [user('newest', 's-2')]
    const out = buildRecallHistory(loadedWindow, [attempt('aged out of the window', 's-1')])
    expect(out).toEqual(['newest', 'aged out of the window'])
    expect(out[out.length - 1]).toBe('aged out of the window')
  })

  /** Retrying a prompt that is already the tail is the ORDINARY shape here: the
   *  send was lost, so ↑ then Enter re-sends the same text. Collapsing it away
   *  kept the entry already in place, whose sidecars belong to a different send
   *  — or, against a transcript row, do not exist — so the offered position has
   *  to carry the NEWER attempt. */
  describe('a same-text attempt collapses by replacing, never by dropping', () => {
    const block = (seq: number, content: string): PasteBlock =>
      ({ id: `p${seq}`, seq, lines: 4, content })

    it('upgrades a transcript row to the attempt repeating it, so its files survive', () => {
      const out = buildRecallEntries(
        [user('resend me', 's-landed')],
        [{ text: 'resend me', sendId: 's-lost', files: ['/tmp/a.pdf'] }],
      )
      // Still ONE position, as the collapse intends — backed by the attempt now
      // rather than by the row, which had no sidecars to give back.
      expect(out.map(e => e.text)).toEqual(['resend me'])
      expect(out[0].attempt?.sendId).toBe('s-lost')
      expect(out[0].attempt?.files).toEqual(['/tmp/a.pdf'])
    })

    it('lets the newest attempt win, so a resend that detached the file offers none', () => {
      const out = buildRecallEntries([], [
        { text: 'same text', sendId: 's-1', files: ['/tmp/removed.pdf'] },
        { text: 'same text', sendId: 's-2' },
      ])
      expect(out.map(e => e.text)).toEqual(['same text'])
      expect(out[0].attempt?.sendId).toBe('s-2')
      expect(out[0].attempt?.files).toBeUndefined()
    })

    it('keeps the newer attempt pastes when the older carried none', () => {
      const pasted = block(1, 'pasted body')
      const out = buildRecallEntries([], [
        { text: 'same text', sendId: 's-1' },
        { text: 'same text', sendId: 's-2', pastes: [pasted] },
      ])
      expect(out).toHaveLength(1)
      expect(out[0].attempt?.pastes).toEqual([pasted])
    })
  })
})
