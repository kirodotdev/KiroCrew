/**
 * A queued send's row carries the paste blocks behind its wire text, so the
 * queue card shows the `[ Paste #N ]` token and not the expanded paste.
 *
 * The gateway queues a send with its WIRE text, in which the composer expanded
 * every paste token in place, and carries no blocks on the entry. The sending
 * browser recorded the send in the paste side table under that wire text
 * (`saveStoredPaste`), which is where the sent bubble is re-collapsed from on a
 * reload; the queue reducers read the same record (`queueEntryPastes`) when a
 * `queue_push` frame or a slot-detail hydration builds the row, keep the blocks
 * an edit's text still holds, and hand them to the user row the entry drains into.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import reducer, { appendQueuedMessage, editQueuedMessage, hydrateQueuedBubbles, queueEntryPastes, removeQueuedMessage } from '../store/chatSlice'
import { buildOutgoingTurn } from '../chat-core/composer/outgoingTurn'
import { formatToken, saveStoredPaste, type PasteBlock } from '../utils/pasteTokens'
import type { ChatMessage } from '../types'

const initial = reducer(undefined, { type: '@@INIT' })
const P1: PasteBlock = { id: 'p1', seq: 1, lines: 3, content: 'alpha\nbeta\ngamma' }
const T1 = formatToken(P1)

/** A real send: the wire the gateway queues, recorded in the side table the
 *  way the composer records it (`storeSentPastes`). */
function sent(text: string) {
  const turn = buildOutgoingTurn({ text, pastes: [P1] }, 'send')
  const entry = turn.storedPaste!
  saveStoredPaste(entry.expanded, entry.display, entry.pastes, entry.files)
  return turn.wire
}

beforeEach(() => { localStorage.clear() })

describe('queueEntryPastes', () => {
  it('finds the blocks this browser recorded under the wire text', () => {
    const wire = sent(`look at ${T1}`)
    expect(wire).toContain('alpha\nbeta\ngamma')
    expect(queueEntryPastes(wire)).toEqual({ pastes: [P1] })
    // The gateway strips trailing whitespace from stored text; the key does too.
    expect(queueEntryPastes(wire + '\n')).toEqual({ pastes: [P1] })
  })

  it('finds nothing for a send another browser made, or one the table evicted', () => {
    const wire = buildOutgoingTurn({ text: `look at ${T1}`, pastes: [P1] }, 'send').wire
    expect(queueEntryPastes(wire)).toEqual({})
  })

  it('finds a paste collapsed at the threshold whose trailing whitespace the wire lost', () => {
    // 199 characters plus one trailing space: collapsed by the composer, but
    // trimmed below the threshold in the text the entry holds.
    const block: PasteBlock = { id: 'p2', seq: 2, lines: 1, content: 'x'.repeat(199) + ' ' }
    saveStoredPaste(block.content, formatToken(block), [block])
    expect(queueEntryPastes(block.content.trimEnd())).toEqual({ pastes: [block] })
  })

  it('returns the blocks in token order, read off the recorded display', () => {
    const long: PasteBlock = { id: 'p2', seq: 2, lines: 4, content: 'alpha\nbeta\ngamma\ndelta' }
    // Pasted #1 then #2, but typed with #2 first: the record's blocks are in
    // paste order and its display in token order.
    const turn = buildOutgoingTurn({ text: `${formatToken(long)}\n${T1}`, pastes: [P1, long] }, 'send')
    expect(turn.storedPaste!.pastes).toEqual([P1, long])
    saveStoredPaste(turn.storedPaste!.expanded, turn.storedPaste!.display, turn.storedPaste!.pastes)
    expect(queueEntryPastes(turn.wire)).toEqual({ pastes: [long, P1] })
  })

  it('finds nothing for empty text', () => {
    expect(queueEntryPastes('')).toEqual({})
  })
})

describe('queue rows carry the blocks', () => {
  it('a queue_push frame builds the row with meta.pastes', () => {
    const wire = sent(`look at ${T1}`)
    const state = reducer({ ...initial, activeSlot: 'chat-1' }, appendQueuedMessage({ slot: 'chat-1', content: wire, ts: 't1', queue_id: 'q-1', meta: { sendId: 's-1' } }))
    const row = state.messages.find(m => m.role === 'queued')
    expect(row?.meta).toEqual({ queueId: 'q-1', sendId: 's-1', pastes: [P1] })
  })

  it('a slot-detail hydration builds the row with meta.pastes', () => {
    const wire = sent(`look at ${T1}`)
    const rows = hydrateQueuedBubbles([], [{ content: wire, queueId: 'q-1', ts: 't1' }])
    expect(rows).toHaveLength(1)
    expect(rows[0].meta).toEqual({ queueId: 'q-1', pastes: [P1] })
  })

  it('a row whose send is not on record carries no pastes key', () => {
    const wire = buildOutgoingTurn({ text: `look at ${T1}`, pastes: [P1] }, 'send').wire
    const rows = hydrateQueuedBubbles([], [{ content: wire, queueId: 'q-1', ts: 't1' }])
    expect(rows[0].meta).toEqual({ queueId: 'q-1' })
  })
})

describe('editQueuedMessage takes the set the card names, and keeps the row\'s blocks otherwise', () => {
  function withQueued(content: string, meta: Record<string, unknown>) {
    return { ...initial, activeSlot: 'chat-1', messages: [{ role: 'queued', content, cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1', ...meta } } as ChatMessage] }
  }
  const long: PasteBlock = { id: 'p2', seq: 2, lines: 4, content: 'alpha\nbeta\ngamma\ndelta' }

  it('an echo frame without a set leaves the row\'s blocks as they are', () => {
    const before = sent(`look at ${T1}`)
    // The echoed content differs from what the card recorded: the gateway
    // renumbered an attachment marker. The paste text is unchanged.
    const after = buildOutgoingTurn({ text: `look at ${T1} now`, pastes: [P1] }, 'send').wire + '\n[attached_file 1] /tmp/b.txt'
    const state = reducer(withQueued(before, { pastes: [P1] }), editQueuedMessage({ slot: 'chat-1', queue_id: 'q-1', content: after }))
    expect(state.messages[0].content).toBe(after)
    expect(state.messages[0].meta).toEqual({ queueId: 'q-1', pastes: [P1] })
  })

  it('takes the kept set the card names: the deleted block goes, though its body is a prefix of the kept one\'s', () => {
    const before = buildOutgoingTurn({ text: `${formatToken(long)}\n${T1}`, pastes: [P1, long] }, 'send').wire
    const state = reducer(withQueued(before, { pastes: [long, P1] }), editQueuedMessage({ slot: 'chat-1', queue_id: 'q-1', content: long.content, pastes: [long] }))
    expect(state.messages[0].meta).toEqual({ queueId: 'q-1', pastes: [long] })
  })

  it('drops the blocks when the card names none kept', () => {
    const before = sent(`look at ${T1}`)
    const state = reducer(withQueued(before, { pastes: [P1] }), editQueuedMessage({ slot: 'chat-1', queue_id: 'q-1', content: 'never mind', pastes: [] }))
    expect(state.messages[0].meta).toEqual({ queueId: 'q-1' })
  })
})

describe('removeQueuedMessage hands the blocks to the drained user row', () => {
  it('the rebuilt user row carries the queued row\'s pastes', () => {
    const wire = buildOutgoingTurn({ text: `look at ${T1}`, pastes: [P1] }, 'send').wire
    const st = { ...initial, activeSlot: 'chat-1', messages: [{ role: 'queued', content: wire, cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1', pastes: [P1] } } as ChatMessage] }
    const state = reducer(st, removeQueuedMessage({ slot: 'chat-1', content: wire, queue_id: 'q-1', meta: { sendId: 's-1' } }))
    const row = state.messages.find(m => m.role === 'user')
    expect(row?.content).toBe(wire)
    expect(row?.meta).toEqual({ pastes: [P1], sendId: 's-1', queueId: 'q-1' })
  })

  it('a queued row without blocks rebuilds as before', () => {
    const wire = 'summarize this'
    const st = { ...initial, activeSlot: 'chat-1', messages: [{ role: 'queued', content: wire, cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1' } } as ChatMessage] }
    const state = reducer(st, removeQueuedMessage({ slot: 'chat-1', content: wire, queue_id: 'q-1' }))
    expect('meta' in (state.messages.find(m => m.role === 'user') as object)).toBe(false)
  })
})
