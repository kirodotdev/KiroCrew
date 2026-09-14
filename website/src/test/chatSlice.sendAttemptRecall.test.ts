import { describe, it, expect } from 'vitest'
import reducer, {
  recordSendAttempt, transferSendAttempt, clearMessages, clearSlotCache, setActiveSlot, confirmOptimisticSend,
  appendMessage, sseChatMessage, hydrateSlotMessages, refreshSlot,
} from '../store/chatSlice'
import { buildRecallEntries, type RecallAttempt, type RecallMessage } from '../lib/recallHistory'

/** Texts only -- a local shim; production has no text-only caller. */
const buildRecallHistory = (m: readonly RecallMessage[], a?: readonly RecallAttempt[]) =>
  buildRecallEntries(m, a).map((e) => e.text)
import { sseSlots } from '../store/dashboardSlice'
import './mockApiClient'

/**
 * Attempt-time prompt recall (`attemptedSends`).
 *
 * ↑/↓ recall was derived purely from `messages`, so it could only offer prompts
 * that reached the transcript. Every way a send is lost also erases the recall
 * entry, and the composer is cleared before any of them can be known — leaving
 * the user's own text nowhere in the UI. These pin the store half: recording is
 * keyed on SUBMISSION, bounded per slot, and dies with its slot.
 */

const SLOT = 'chat-1-1700000000'
const OTHER = 'chat-2-1700000001'
const init = () => reducer(undefined, { type: '@@INIT' })

/** Send ids, distinct per call, as `mintSendId` produces at the real call site. */
let seq = 0
const rec = (slot: string, text: string) => recordSendAttempt({ slot, text, sendId: `s-${++seq}` })
const texts = (s: ReturnType<typeof init>, slot: string) =>
  s.attemptedSends[slot]?.map(a => a.text)

describe('recordSendAttempt', () => {
  it('retains a submitted prompt under its slot', () => {
    const s = reducer(init(), rec(SLOT, 'first'))
    expect(texts(s, SLOT)).toEqual(['first'])
  })

  it('records the send id the recall merge matches on', () => {
    const s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'tagged', sendId: 's-42' }))
    expect(s.attemptedSends[SLOT]).toEqual([{ text: 'tagged', sendId: 's-42' }])
  })

  it('keeps order oldest to newest, matching the transcript half', () => {
    let s = init()
    for (const text of ['one', 'two', 'three']) s = reducer(s, rec(SLOT, text))
    expect(texts(s, SLOT)).toEqual(['one', 'two', 'three'])
  })

  it('collapses a consecutive duplicate', () => {
    let s = reducer(init(), rec(SLOT, 'same'))
    s = reducer(s, rec(SLOT, 'same'))
    expect(texts(s, SLOT)).toEqual(['same'])
  })

  it('gives the collapsed entry the newer send id, not the first one', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'same', sendId: 's-1' }))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'same', sendId: 's-2' }))
    expect(s.attemptedSends[SLOT]).toEqual([{ text: 'same', sendId: 's-2' }])
  })

  it('retains a repeat that is not consecutive, as a shell does', () => {
    let s = init()
    for (const text of ['a', 'b', 'a']) s = reducer(s, rec(SLOT, text))
    expect(texts(s, SLOT)).toEqual(['a', 'b', 'a'])
  })

  it('does not let one slot see another slot prompts', () => {
    let s = reducer(init(), rec(SLOT, 'mine'))
    s = reducer(s, rec(OTHER, 'theirs'))
    expect(texts(s, SLOT)).toEqual(['mine'])
    expect(texts(s, OTHER)).toEqual(['theirs'])
  })

  it('bounds retention and drops the oldest first', () => {
    let s = init()
    for (let i = 0; i < 60; i++) s = reducer(s, rec(SLOT, `p${i}`))
    const list = texts(s, SLOT)!
    expect(list).toHaveLength(50)
    expect(list[0]).toBe('p10')
    expect(list[list.length - 1]).toBe('p59')
  })

  it('ignores an empty prompt and an absent slot', () => {
    let s = reducer(init(), rec(SLOT, ''))
    s = reducer(s, rec('', 'orphan'))
    expect(s.attemptedSends).toEqual({})
  })

  it('refuses a prototype-polluting slot key', () => {
    const s = reducer(init(), rec('__proto__', 'hostile'))
    expect(s.attemptedSends).toEqual({})
    expect(({} as Record<string, unknown>).hostile).toBeUndefined()
  })

  it('drops a slot entry when an authoritative list no longer carries the slot', () => {
    let s = reducer(init(), rec(SLOT, 'stale'))
    expect(texts(s, SLOT)).toEqual(['stale'])
    s = reducer(s, sseSlots([{ key: OTHER }] as never))
    expect(s.attemptedSends[SLOT]).toBeUndefined()
  })
})

/**
 * The composer clears its text, its staged files and its collapsed pastes in one
 * statement, so a record carrying text alone leaves ↑ able to restore a prompt
 * that then resends with its attachments dropped and its paste tokens expanded
 * against nothing — the tokens go out literally. The sidecars ride with the
 * prompt so the restore can put the composer back as it was.
 */
describe('a recorded prompt carries the sidecars cleared alongside it', () => {
  const block = { id: 'p1', text: 'pasted body' } as never

  it('retains the staged files and collapsed pastes', () => {
    const s = reducer(init(), recordSendAttempt({
      slot: SLOT, text: 'with sidecars', sendId: 's-1', files: ['a.txt'], pastes: [block],
    }))
    expect(s.attemptedSends[SLOT][0].files).toEqual(['a.txt'])
    expect(s.attemptedSends[SLOT][0].pastes).toEqual([block])
  })

  it('omits an absent or empty sidecar rather than storing an empty array', () => {
    const s = reducer(init(), recordSendAttempt({
      slot: SLOT, text: 'no sidecars', sendId: 's-1', files: [], pastes: [],
    }))
    expect(s.attemptedSends[SLOT][0]).toEqual({ text: 'no sidecars', sendId: 's-1' })
  })

  it('carries them to the slot a new-session send actually reached', () => {
    let s = reducer(init(), recordSendAttempt({
      slot: SLOT, text: 'moved', sendId: 's-1', files: ['a.txt'], pastes: [block],
    }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: OTHER, sendId: 's-1' }))
    expect(s.attemptedSends[SLOT]).toEqual([])
    expect(s.attemptedSends[OTHER][0].files).toEqual(['a.txt'])
    expect(s.attemptedSends[OTHER][0].pastes).toEqual([block])
  })
})

/**
 * A cleared conversation must not come back through recall.
 *
 * `/clear` empties the transcript but does NOT delete the slot, so it never
 * reaches the `slotKeyedMaps` eviction that retires a slot's state. Recall
 * reads attempts even when `messages` is empty, so an attempt surviving the
 * clear would put discarded text back in the composer on the next ↑. Both
 * clear reducers evict it, exactly as they already evict `thinkingOrphans`.
 */
describe('clearing a conversation retires its recorded attempts', () => {
  it('clearMessages drops the attempts of the slot being viewed', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, rec(SLOT, 'discarded'))
    expect(texts(s, SLOT)).toEqual(['discarded'])
    s = reducer(s, clearMessages())
    expect(s.attemptedSends[SLOT]).toBeUndefined()
  })

  it('clearMessages leaves an unrelated slot recoverable', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, rec(SLOT, 'discarded'))
    s = reducer(s, rec(OTHER, 'untouched'))
    s = reducer(s, clearMessages())
    expect(s.attemptedSends[SLOT]).toBeUndefined()
    expect(texts(s, OTHER)).toEqual(['untouched'])
  })

  it('clearSlotCache drops the attempts of a slot cleared in the background', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, rec(OTHER, 'discarded'))
    s = reducer(s, clearSlotCache(OTHER))
    expect(s.attemptedSends[OTHER]).toBeUndefined()
  })

  it('clearSlotCache leaves the viewed slot recoverable', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, rec(SLOT, 'untouched'))
    s = reducer(s, rec(OTHER, 'discarded'))
    s = reducer(s, clearSlotCache(OTHER))
    expect(texts(s, SLOT)).toEqual(['untouched'])
    expect(s.attemptedSends[OTHER]).toBeUndefined()
  })
})

/** A transcript row as the server persists it, carrying the send's own id. */
const row = (content: string, sendId: string): RecallMessage =>
  ({ role: 'user', content, meta: { sendId } })
/** A row as a FETCHED page carries it: server identity (`mid`) alongside the id. */
const serverRow = (content: string, sendId: string, mid: string) =>
  ({ role: 'user', content, cls: '', ts: '2026-01-01T00:00:00Z', meta: { sendId, mid } })
/** The payload `refreshSlot.fulfilled` delivers: one server page for one slot. */
const refreshPage = (key: string, messages: ReturnType<typeof serverRow>[]) =>
  refreshSlot.fulfilled(
    {
      key, messages, running: false, stopping: false, hasMore: false,
      total: messages.length, queue: [], nextBefore: 0, context: undefined,
    } as never,
    'req-refresh', key,
  )
const WIRE_COMPOSER = 'look at @notes.txt'
const WIRE_PERSISTED = 'look at [attached_file 1] /home/u/notes.txt'

/**
 * A fetched page suppresses a prompt without erasing its record.
 *
 * Deleting the record on the page that proves the prompt landed reads as tidy
 * and loses data: two same-slot refreshes can fulfil out of network order, so
 * the newer page's delete can land moments before the older page drops the row,
 * and the prompt is then in neither the transcript nor recall. The record
 * survives every refresh instead, and the builder withholds it for exactly as
 * long as a loaded row carries its send id.
 */
describe('a fetched page suppresses an attempt without erasing it', () => {
  it('keeps the record and still offers the prompt only once', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'landed', sendId: 's-1' }))
    s = reducer(s, refreshPage(SLOT, [serverRow('landed', 's-1', 'm-1')]))
    expect(texts(s, SLOT)).toEqual(['landed'])
    expect(buildRecallHistory(s.messages, s.attemptedSends[SLOT])).toEqual(['landed'])
  })

  it('recovers the prompt when an older refresh drops the row a newer one carried', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'at risk', sendId: 's-9' }))
    s = reducer(s, refreshPage(SLOT, [serverRow('at risk', 's-9', 'm-9')]))
    s = reducer(s, refreshPage(SLOT, [serverRow('earlier', 's-8', 'm-8')]))
    expect(s.messages.some(m => m.meta?.sendId === 's-9')).toBe(false)
    const out = buildRecallHistory(s.messages, s.attemptedSends[SLOT])
    expect(out[out.length - 1]).toBe('at risk')
  })

  it('keeps an attempt the page never mentions', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'lost', sendId: 's-2' }))
    s = reducer(s, refreshPage(SLOT, [serverRow('earlier', 's-1', 'm-1')]))
    expect(texts(s, SLOT)).toEqual(['lost'])
  })

  it('keeps a background slot record across a hydrated page', () => {
    let s = reducer(init(), setActiveSlot(OTHER))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'landed', sendId: 's-4' }))
    s = reducer(s, hydrateSlotMessages({
      slot: SLOT, messages: [serverRow('landed', 's-4', 'm-4')] as never, hasMore: false,
    }))
    expect(texts(s, SLOT)).toEqual(['landed'])
  })
})

/**
 * A send receipt is not proof the prompt survives locally.
 *
 * The receipt says the server took the text, but the row rendering it is still
 * client-only until a page carries it back. A refresh whose snapshot was taken
 * before the send rebuilds the transcript without that row, so retiring on the
 * receipt would discard the record and the row together and the prompt would be
 * nowhere — the loss this whole feature exists to prevent, reintroduced by its
 * own cleanup. The attempt therefore survives a receipt.
 */
describe('an attempt outlives a receipt whose row a stale page then drops', () => {
  it('still recovers the prompt after the receipt and a stale page', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'at risk', sendId: 's-9' }))
    s = reducer(s, appendMessage({ role: 'user', content: 'at risk', cls: '', meta: { sendId: 's-9' } } as never))
    s = reducer(s, confirmOptimisticSend({ slot: SLOT, sendId: 's-9', mid: 'm-9' }))
    s = reducer(s, refreshPage(SLOT, [serverRow('earlier', 's-8', 'm-8')]))
    expect(s.messages.some(m => m.meta?.sendId === 's-9')).toBe(false)
    const out = buildRecallHistory(s.messages, s.attemptedSends[SLOT])
    expect(out[out.length - 1]).toBe('at risk')
  })

  it('does not retire on the receipt alone', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'at risk', sendId: 's-9' }))
    s = reducer(s, confirmOptimisticSend({ slot: SLOT, sendId: 's-9', mid: 'm-9' }))
    expect(texts(s, SLOT)).toEqual(['at risk'])
  })

  it('does not retire on a server echo alone', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'at risk', sendId: 's-9' }))
    s = reducer(s, appendMessage({ role: 'user', content: 'at risk', cls: '', meta: { sendId: 's-9' } } as never))
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'user', content: 'at risk', ts: '2026-01-01T00:00:00Z',
      meta: { sendId: 's-9', mid: 'm-9' },
    }))
    expect(texts(s, SLOT)).toEqual(['at risk'])
  })
})

/**
 * The case that makes the collapsed entry adopt the newer send id.
 *
 * A prompt lands, the user submits the same text again, and THAT send is the one
 * lost. Both submissions collapse to a single entry, so the id it carries is the
 * whole answer to "did this land?" — and the first send's id is on a transcript
 * row. Keeping it would judge the prompt on screen and withhold the text the
 * user just watched vanish, which is the failure recall exists to prevent. The
 * attachment spelling makes it visible: ↑ would return the server's wire form
 * instead of what the composer held.
 */
describe('a lost resend of an already-landed prompt stays recoverable', () => {
  it('offers the composer spelling when the resend is the lost one', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: WIRE_COMPOSER, sendId: 's-1' }))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: WIRE_COMPOSER, sendId: 's-2' }))
    const out = buildRecallHistory([row(WIRE_PERSISTED, 's-1')], s.attemptedSends[SLOT])
    expect(out).toEqual([WIRE_PERSISTED, WIRE_COMPOSER])
    expect(out[out.length - 1]).toBe(WIRE_COMPOSER)
  })

  it('offers it even after a page proved the earlier send landed', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: WIRE_COMPOSER, sendId: 's-1' }))
    s = reducer(s, refreshPage(SLOT, [serverRow(WIRE_PERSISTED, 's-1', 'm-1')]))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: WIRE_COMPOSER, sendId: 's-2' }))
    const out = buildRecallHistory([row(WIRE_PERSISTED, 's-1')], s.attemptedSends[SLOT])
    expect(out[out.length - 1]).toBe(WIRE_COMPOSER)
  })

  it('stops shadowing the newest prompt once the resend itself lands', () => {
    let s = reducer(init(), setActiveSlot(SLOT))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'recovered', sendId: 's-1' }))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'recovered', sendId: 's-2' }))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'newest', sendId: 's-3' }))
    s = reducer(s, refreshPage(SLOT, [
      serverRow('recovered', 's-2', 'm-2'), serverRow('newest', 's-3', 'm-3'),
    ]))
    expect(texts(s, SLOT)).toEqual(['recovered', 'newest'])
    const messages = [row('recovered', 's-2'), row('newest', 's-3')]
    const out = buildRecallHistory(messages, s.attemptedSends[SLOT])
    expect(out).toEqual(['recovered', 'newest'])
    expect(out[out.length - 1]).toBe('newest')
  })
})

/**
 * Moving a record to the slot its send reached.
 *
 * A session-creating send is recorded before the create is awaited, because a
 * rejection there unwinds the send while the composer is already cleared. So the
 * record starts under the slot the user left, and has to follow the prompt once
 * the create names the real one.
 */
describe('transferSendAttempt', () => {
  it('moves the recorded prompt to the slot the send reached', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'moved', sendId: 's-1' }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: OTHER, sendId: 's-1' }))
    expect(texts(s, OTHER)).toEqual(['moved'])
    expect(texts(s, SLOT) ?? []).toEqual([])
  })

  it('carries the send id across, so a landed row still suppresses the entry', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'moved', sendId: 's-1' }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: OTHER, sendId: 's-1' }))
    expect(s.attemptedSends[OTHER]?.[0]?.sendId).toBe('s-1')
  })

  it('moves only the named send, leaving the origin its other prompts', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'stays', sendId: 's-1' }))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'goes', sendId: 's-2' }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: OTHER, sendId: 's-2' }))
    expect(texts(s, SLOT)).toEqual(['stays'])
    expect(texts(s, OTHER)).toEqual(['goes'])
  })

  it('appends to the destination rather than replacing what it holds', () => {
    let s = reducer(init(), recordSendAttempt({ slot: OTHER, text: 'already there', sendId: 's-1' }))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'arriving', sendId: 's-2' }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: OTHER, sendId: 's-2' }))
    expect(texts(s, OTHER)).toEqual(['already there', 'arriving'])
  })

  /** The origin can be cleared, or its slot evicted, while the create is in
   *  flight. The prompt is then recorded nowhere rather than mis-filed. */
  it('is inert when the origin no longer holds that send', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'kept', sendId: 's-1' }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: OTHER, sendId: 's-absent' }))
    expect(texts(s, SLOT)).toEqual(['kept'])
    expect(texts(s, OTHER) ?? []).toEqual([])
  })

  it('is inert when the origin and destination are the same slot', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'kept', sendId: 's-1' }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: SLOT, sendId: 's-1' }))
    expect(texts(s, SLOT)).toEqual(['kept'])
  })

  it('refuses a prototype-polluting slot key on either side', () => {
    let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'kept', sendId: 's-1' }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: '__proto__', sendId: 's-1' }))
    expect(texts(s, SLOT)).toEqual(['kept'])
    s = reducer(s, transferSendAttempt({ from: '__proto__', to: OTHER, sendId: 's-1' }))
    expect(texts(s, OTHER) ?? []).toEqual([])
  })

  it('holds the destination inside its retention bound', () => {
    let s = init()
    for (let i = 0; i < 50; i++) s = reducer(s, recordSendAttempt({ slot: OTHER, text: `p${i}`, sendId: `d-${i}` }))
    s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'arriving', sendId: 's-x' }))
    s = reducer(s, transferSendAttempt({ from: SLOT, to: OTHER, sendId: 's-x' }))
    expect(s.attemptedSends[OTHER]).toHaveLength(50)
    expect(texts(s, OTHER)?.[49]).toBe('arriving')
    expect(texts(s, OTHER)).not.toContain('p0')
  })

  /** An attempt with no sidecars OMITS those keys rather than storing empties, so
   *  a field-by-field merge could not express their absence: the collapsed record
   *  inherited the older send's, and ↑ then re-staged — and resent — a file the
   *  user had detached. Replacing the record outright is what makes the newer
   *  send's own sidecars, INCLUDING having none, the ones recall offers. */
  describe('collapsing a repeated prompt replaces the record', () => {
    const lastOf = (s: ReturnType<typeof init>) => s.attemptedSends[SLOT]?.slice(-1)[0]

    it('does not inherit the files of the send it collapses into', () => {
      let s = reducer(init(), recordSendAttempt({
        slot: SLOT, text: 'ship it', sendId: 's-1', files: ['/tmp/detached.pdf'],
      }))
      expect(lastOf(s)?.files).toEqual(['/tmp/detached.pdf'])
      // Same text, attachment removed before the retry.
      s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'ship it', sendId: 's-2' }))
      expect(s.attemptedSends[SLOT]).toHaveLength(1)
      expect(lastOf(s)?.sendId).toBe('s-2')
      expect(lastOf(s)?.files).toBeUndefined()
    })

    it('does not inherit the pastes of the send it collapses into', () => {
      const pasted = { id: 'p1', seq: 1, lines: 9, content: 'body' }
      let s = reducer(init(), recordSendAttempt({
        slot: SLOT, text: 'ship it', sendId: 's-1', pastes: [pasted],
      }))
      s = reducer(s, recordSendAttempt({ slot: SLOT, text: 'ship it', sendId: 's-2' }))
      expect(lastOf(s)?.pastes).toBeUndefined()
    })

    it('carries the newer sidecars in when the older send had none', () => {
      let s = reducer(init(), recordSendAttempt({ slot: SLOT, text: 'ship it', sendId: 's-1' }))
      s = reducer(s, recordSendAttempt({
        slot: SLOT, text: 'ship it', sendId: 's-2', files: ['/tmp/added.pdf'],
      }))
      expect(s.attemptedSends[SLOT]).toHaveLength(1)
      expect(lastOf(s)?.files).toEqual(['/tmp/added.pdf'])
    })
  })
})
