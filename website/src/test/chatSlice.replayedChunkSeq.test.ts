import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  sseChatMessage, setActiveSlot, refreshSlot, switchSlot, warmSlotCache, snapshotChunkGen,
  snapshotChunkSeq,
} from '../store/chatSlice'
import './mockApiClient'

/**
 * A slot snapshot seeds the replayed-chunk guard.
 *
 * After a WebSocket drop the client refetches the slot; the snapshot's trailing
 * `streaming` row already holds every chunk the server had emitted, and now
 * carries the newest chunk `seq` folded into it (chat_utils._prepare_messages).
 * A live `chat_chunk` that raced the snapshot arrives with a seq at or below
 * that floor and used to be appended a second time — the duplicated leading
 * fragment. The three slot-detail reducers seed `lastChunkSeq` from the row so
 * the existing `seq <= lastChunkSeq` guard drops it. A snapshot without `seq`
 * (older gateway) leaves the guard untouched.
 */

const SLOT = 'chat-active'
const OTHER = 'chat-bg'

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer } })
}


type TestStore = ReturnType<typeof makeStore>

function registeredRefresh(
  store: TestStore,
  payload: Parameters<typeof refreshSlot.fulfilled>[0],
  requestId: string,
  arg: Parameters<typeof refreshSlot.fulfilled>[2],
) {
  if (store.getState().chat.refreshIssueByRequest[requestId] === undefined) {
    store.dispatch(refreshSlot.pending(requestId, arg))
  }
  return refreshSlot.fulfilled(payload, requestId, arg)
}

function registeredSwitch(
  store: TestStore,
  payload: Parameters<typeof switchSlot.fulfilled>[0],
  requestId: string,
  arg: Parameters<typeof switchSlot.fulfilled>[2],
) {
  const chat = store.getState().chat
  const ownsCurrent = chat.slotSwitchChunkClaim?.requestId === requestId
    && chat.slotSwitchChunkClaim.target === payload.key
  if (!ownsCurrent && chat.slotSwitchChunkClaim == null && chat.slotSwitchRequestId === null) {
    store.dispatch(switchSlot.pending(requestId, arg))
  }
  return switchSlot.fulfilled(payload, requestId, arg)
}
type Row = { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown>; seq?: number; gen?: string }

function slotPayload(key: string, messages: Row[], running = true) {
  return { key, messages, running, hasMore: false, total: messages.length, queue: [], stopping: false }
}

const midStream = (seq?: number): Row[] => [
  { role: 'user', content: 'hi', cls: '', ts: '2026-09-08T10:00:00Z', meta: { mid: 'u1' } },
  { role: 'streaming', content: 'Hello wor', cls: 'msg msg-a', ...(seq === undefined ? {} : { seq }) },
]

const text = (s: ReturnType<ReturnType<typeof makeStore>['getState']>) =>
  s.chat.messages.filter(m => m.role === 'streaming' || m.role === 'assistant').map(m => m.content).join('')

describe('page segment replay authority', () => {
  it('derives seq/gen only while the fetched page segment is open', () => {
    const open = midStream(7).map(m => ({ ...m, cls: m.cls ?? '', gen: 'g1' }))
    expect(snapshotChunkSeq(open)).toBe(7)
    expect(snapshotChunkGen(open)).toBe('g1')

    const finalized: Row[] = [{ role: 'assistant', content: 'done', cls: '', seq: 9, gen: 'g1' }]
    expect(snapshotChunkSeq(finalized)).toBeUndefined()
    expect(snapshotChunkGen(finalized)).toBeUndefined()

    const absent: Row[] = [{ role: 'user', content: 'waiting', cls: '' }]
    expect(snapshotChunkSeq(absent)).toBeUndefined()
    expect(snapshotChunkGen(absent)).toBeUndefined()
    expect(snapshotChunkSeq(midStream().map(m => ({ ...m, cls: m.cls ?? '' }))))
      .toBeUndefined()
  })
})

describe('refreshSlot.fulfilled seeds the replay guard (reconnect path)', () => {
  it('drops a replayed chunk at or below the snapshot seq and keeps the next one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, midStream(3)), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBe(3)

    // The frames that raced the snapshot: already inside "Hello wor".
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Hello ', seq: 2 }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'wor', seq: 3 }))
    expect(text(store.getState())).toBe('Hello wor')

    // Forward progress is still applied.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'ld', seq: 4 }))
    expect(text(store.getState())).toBe('Hello world')
    expect(store.getState().chat.lastChunkSeq).toBe(4)
  })

  it('leaves the guard alone for a snapshot without seq (older gateway)', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, midStream()), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBeUndefined()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'ld', seq: 4 }))
    expect(text(store.getState())).toBe('Hello world')
  })

  it('never lowers a floor a live frame already moved past', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Hello world!', seq: 5 }))
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, midStream(3)), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBe(5)
  })

  it('does not seed from an idle snapshot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, midStream(3), false), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBeUndefined()
  })
})

describe('seqs are the slot\'s, so the floor survives an unobserved turn boundary', () => {
  it('an idle snapshot clears the floor (a restarted gateway numbers from 0 again)', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old turn', seq: 9 }))
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, [{ role: 'assistant', content: 'old turn', cls: 'msg msg-a' }], false), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBeUndefined()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'new turn', seq: 1 }))
    expect(text(store.getState())).toContain('new turn')
  })

  it('the next turn\'s chunks apply over a floor a lost _done left behind', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old', seq: 9 }))
    // No `_done`, no user frame seen: the next turn's first chunk is still
    // numbered above the floor, because the counter is the slot's.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'fresh', seq: 10, batched: true, parts: [{ seq: 10, text: 'fresh' }] }))
    expect(text(store.getState())).toContain('fresh')
  })

  it('a snapshot from an earlier turn cannot raise the floor over the live position', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'live', seq: 12 }))
    // A refresh requested during the previous turn lands now.
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, midStream(9), true), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBe(12)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: '!', seq: 13, batched: true, parts: [{ seq: 13, text: '!' }] }))
    expect(text(store.getState())).toContain('!')
  })
})

describe('a gateway restart (new generation) replaces the floor', () => {
  it('a chunk from a new generation applies over a higher floor from the old one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old', seq: 57, gen: 'g1' }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'fresh', seq: 3, gen: 'g2', batched: true, parts: [{ seq: 3, text: 'fresh' }] }))
    expect(text(store.getState())).toContain('fresh')
    expect(store.getState().chat.lastChunkSeq).toBe(3)
    expect(store.getState().chat.lastChunkGen).toBe('g2')
  })

  it('a same-generation replay is still dropped', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old', seq: 57, gen: 'g1' }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'dup', seq: 3, gen: 'g1', batched: true, parts: [{ seq: 3, text: 'dup' }] }))
    expect(text(store.getState())).not.toContain('dup')
  })

  it('a running snapshot from a new generation replaces the floor rather than raising it', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old', seq: 57, gen: 'g1' }))
    // The gateway restarted and a new turn is already streaming (seq 3) when the reconnect refresh lands.
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, [{ role: 'streaming', content: 'abc', cls: 'msg msg-a', seq: 3, gen: 'g2' }], true), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBe(3)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'd', seq: 4, gen: 'g2', batched: true, parts: [{ seq: 4, text: 'd' }] }))
    expect(text(store.getState())).toBe('abcd')
  })

  it('a background pane replaces its floor on a new generation too', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: OTHER, role: 'chunk', content: 'old', seq: 57, gen: 'g1' }))
    store.dispatch(warmSlotCache.fulfilled({ ...slotPayload(OTHER, [{ role: 'streaming', content: 'abc', cls: 'msg msg-a', seq: 3, gen: 'g2' }]), warmSeq: 1 }, 'w1', OTHER))
    expect(store.getState().chat.slotRun[OTHER]?.lastChunkSeq).toBe(3)
    expect(store.getState().chat.slotRun[OTHER]?.lastChunkGen).toBe('g2')
  })

  it('a new generation replaces a floor that carries NO generation', () => {
    // The upgrade case: an older gateway's seq-only frames leave a floor with no
    // `gen`, then the upgraded process's first stamped chunk arrives numbered from
    // a restarted counter. Treating "no generation" as compatible left that stale
    // floor in place and dropped the new process's reply text.
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old', seq: 57 }))
    expect(store.getState().chat.lastChunkSeq).toBe(57)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'fresh', seq: 3, gen: 'g2', batched: true, parts: [{ seq: 3, text: 'fresh' }] }))
    expect(text(store.getState())).toContain('fresh')
    expect(store.getState().chat.lastChunkSeq).toBe(3)
    expect(store.getState().chat.lastChunkGen).toBe('g2')
  })

  it('a snapshot without a generation (older gateway) keeps ordering by seq', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'live', seq: 12, gen: 'g1' }))
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, midStream(9), true), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBe(12)
  })
})

describe('gap markers are derived after the snapshot floor is applied', () => {
  it('a gap the snapshot filled in is not flagged', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    // Chunks 1..3 applied live; chunk 4 is lost on the wire; the refresh lands
    // with the streaming row folded up to seq 4, then the frame holding 5 arrives.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'abc', seq: 3, batched: true, parts: [{ seq: 1, text: 'a' }, { seq: 2, text: 'b' }, { seq: 3, text: 'c' }] }))
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, [{ role: 'streaming', content: 'abcd', cls: 'msg msg-a', seq: 4 }], true), 'r1', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'e', seq: 5, batched: true, parts: [{ seq: 5, text: 'e' }] }))
    expect(text(store.getState())).toBe('abcde')
  })

  it('a gap that is still open after filtering is flagged once, between the kept parts', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'ab', seq: 2, batched: true, parts: [{ seq: 1, text: 'a' }, { seq: 2, text: 'b' }] }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'ce', seq: 5, batched: true, parts: [{ seq: 3, text: 'c' }, { seq: 5, text: 'e' }] }))
    expect(text(store.getState())).toBe('abc\n[1 chunk(s) missed]\ne')
  })

  it('a gap between the floor and the first kept part is flagged', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(registeredRefresh(store, slotPayload(SLOT, midStream(3), true), 'r1', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'ld', seq: 5, batched: true, parts: [{ seq: 5, text: 'ld' }] }))
    expect(text(store.getState())).toBe('Hello wor\n[1 chunk(s) missed]\nld')
  })
})

describe('switchSlot.fulfilled seeds the replay guard', () => {
  it('keeps a newer fetched stream and admits the next chunk', () => {
    const store = makeStore()
    const anchor: Row = {
      role: 'user',
      content: 'question',
      cls: '',
      ts: '2026-09-08T10:00:00Z',
      meta: { mid: 'question-1' },
    }
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, ...anchor }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial', seq: 1, gen: 'g1' }))
    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'user',
      content: 'confirmed follow-up',
      meta: { sendId: 'send-1', mid: 'user-1' },
    }))
    store.dispatch(switchSlot.pending('away', OTHER))
    store.dispatch(switchSlot.pending('switch', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' local', seq: 2, gen: 'g1' }))

    // The row itself still has no seq 2 stamp; the active floor carries that
    // live authority. This snapshot has advanced through seq 3 and must win.
    store.dispatch(registeredSwitch(store, slotPayload(SLOT, [
      anchor,
      { role: 'streaming', content: 'partial local fetched', cls: 'msg msg-a', seq: 3, gen: 'g1' },
    ], true), 'switch', SLOT))
    expect(store.getState().chat.messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'question'],
      ['streaming', 'partial local fetched'],
      ['user', 'confirmed follow-up'],
    ])
    expect(store.getState().chat.lastChunkSeq).toBe(3)
    expect(store.getState().chat.lastChunkGen).toBe('g1')

    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' next', seq: 4, gen: 'g1' }))
    expect(store.getState().chat.messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'question'],
      ['streaming', 'partial local fetched next'],
      ['user', 'confirmed follow-up'],
    ])
    expect(store.getState().chat.lastChunkSeq).toBe(4)
  })

  it('keeps a stream updated during fetch ahead of a confirmed user tail', () => {
    const store = makeStore()
    const anchor: Row = {
      role: 'user',
      content: 'question',
      cls: '',
      ts: '2026-09-08T10:00:00Z',
      meta: { mid: 'question-1' },
    }
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, ...anchor }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial', seq: 1, gen: 'g1' }))
    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'user',
      content: 'confirmed follow-up',
      meta: { sendId: 'send-1', mid: 'user-1' },
    }))

    // Leave and switch back so pending restores [anchor, streaming, confirmed user]
    // while its slot-detail request is in flight.
    store.dispatch(switchSlot.pending('away', OTHER))
    store.dispatch(switchSlot.pending('switch', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' live', seq: 2, gen: 'g1' }))

    // The running snapshot includes the anchor and a stale copy of the stream,
    // but predates both the live suffix and the confirmed follow-up.
    store.dispatch(registeredSwitch(store, slotPayload(SLOT, [
      anchor,
      { role: 'streaming', content: 'partial', cls: 'msg msg-a', seq: 1, gen: 'g1' },
    ], true), 'switch', SLOT))
    expect(store.getState().chat.messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'question'],
      ['streaming', 'partial live'],
      ['user', 'confirmed follow-up'],
    ])
    expect(store.getState().chat.lastChunkSeq).toBe(2)

    // The floor retained across the stale fetch must still admit forward progress
    // into the same mid-array stream rather than suppressing or splitting it.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' next', seq: 3, gen: 'g1' }))
    expect(store.getState().chat.messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'question'],
      ['streaming', 'partial live next'],
      ['user', 'confirmed follow-up'],
    ])
    expect(store.getState().chat.lastChunkSeq).toBe(3)
  })

  it('keeps the live stream before a canonical confirmed user row from the page', () => {
    const store = makeStore()
    const anchor: Row = {
      role: 'user',
      content: 'question',
      cls: '',
      ts: '2026-09-08T10:00:00Z',
      meta: { mid: 'question-1' },
    }
    const confirmed: Row = {
      role: 'user',
      content: 'confirmed follow-up',
      cls: '',
      ts: '2026-09-08T10:00:01Z',
      meta: { sendId: 'send-1', mid: 'user-1' },
    }
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, ...anchor }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'partial', seq: 1 }))
    store.dispatch(sseChatMessage({ slot: SLOT, ...confirmed }))
    store.dispatch(switchSlot.pending('away', OTHER))
    store.dispatch(switchSlot.pending('switch', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' live', seq: 2 }))

    // This snapshot has caught up to the confirmed send but not the live chunk.
    // Its canonical user row carries no client-only persistence proof.
    store.dispatch(registeredSwitch(store, slotPayload(SLOT, [
      anchor,
      { role: 'streaming', content: 'partial', cls: 'msg msg-a', seq: 1 },
      confirmed,
    ], true), 'switch', SLOT))
    expect(store.getState().chat.messages.map(m => [m.role, m.content])).toEqual([
      ['user', 'question'],
      ['streaming', 'partial live'],
      ['user', 'confirmed follow-up'],
    ])
    expect(store.getState().chat.lastChunkSeq).toBe(2)
  })

  it('keeps one canonical assistant when the page finalized during the fetch', () => {
    const store = makeStore()
    const anchor: Row = {
      role: 'user',
      content: 'question',
      cls: '',
      ts: '2026-09-08T10:00:00Z',
      meta: { mid: 'question-1' },
    }
    const canonical: Row = {
      role: 'assistant',
      content: 'partial complete',
      cls: 'msg msg-a',
      ts: '2026-09-08T10:00:01Z',
      meta: { mid: 'answer-1' },
    }
    const confirmed: Row = {
      role: 'user',
      content: 'confirmed follow-up',
      cls: '',
      ts: '2026-09-08T10:00:02Z',
      meta: { sendId: 'send-1', mid: 'user-1' },
    }
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, ...anchor }))
    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: 'partial',
      seq: 1,
      gen: 'g1',
    }))
    store.dispatch(sseChatMessage({ slot: SLOT, ...confirmed }))
    store.dispatch(switchSlot.pending('away', OTHER))
    store.dispatch(switchSlot.pending('switch', SLOT))
    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'chunk',
      content: ' complete',
      seq: 2,
      gen: 'g1',
    }))
    expect(store.getState().chat._wsChunkedDuringFetch).toBe(true)

    store.dispatch(registeredSwitch(store,
      slotPayload(SLOT, [anchor, canonical, confirmed], true), 'switch', SLOT,
    ))

    expect(store.getState().chat.messages.map(m => [m.role, m.content, m.meta?.mid]))
      .toEqual([
        ['user', 'question', 'question-1'],
        ['assistant', 'partial complete', 'answer-1'],
        ['user', 'confirmed follow-up', 'user-1'],
      ])
    expect(store.getState().chat.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(store.getState().chat.messages.filter(m => m.meta?.mid === 'answer-1'))
      .toHaveLength(1)
  })

  it('keeps a newer same-text assistant with a different row id', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({
      slot: SLOT,
      role: 'assistant',
      content: 'Done.',
      ts: '2026-09-08T10:00:02Z',
      meta: { mid: 'answer-new' },
    }))

    store.dispatch(registeredSwitch(store, slotPayload(SLOT, [{
      role: 'assistant',
      content: 'Done.',
      cls: 'msg msg-a',
      ts: '2026-09-08T10:00:01Z',
      meta: { mid: 'answer-old' },
    }], true), 'same-text', SLOT))

    expect(store.getState().chat.messages
      .filter(m => m.role === 'assistant')
      .map(m => m.meta?.mid))
      .toEqual(['answer-old', 'answer-new'])
  })

  it('prefers a fetched stream from a new generation', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old', seq: 9, gen: 'g1' }))
    store.dispatch(switchSlot.pending('away', OTHER))
    store.dispatch(switchSlot.pending('switch', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' local', seq: 10, gen: 'g1' }))

    store.dispatch(registeredSwitch(store, slotPayload(SLOT, [
      { role: 'streaming', content: 'fresh', cls: 'msg msg-a', seq: 1, gen: 'g2' },
    ], true), 'switch', SLOT))
    expect(text(store.getState())).toBe('fresh')
    expect(store.getState().chat.lastChunkSeq).toBe(1)
    expect(store.getState().chat.lastChunkGen).toBe('g2')

    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' next', seq: 2, gen: 'g2' }))
    expect(text(store.getState())).toBe('fresh next')
  })

  it('keeps both segments when a same-generation snapshot has no seq or shared anchor', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'local', seq: 2, gen: 'g1' }))
    store.dispatch(switchSlot.pending('away', OTHER))
    store.dispatch(switchSlot.pending('switch', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: ' newer', seq: 3, gen: 'g1' }))

    store.dispatch(registeredSwitch(store, slotPayload(SLOT, [
      { role: 'streaming', content: 'unvouched snapshot', cls: 'msg msg-a', gen: 'g1' },
    ], true), 'switch', SLOT))
    expect(store.getState().chat.messages.map(message => [message.role, message.content])).toEqual([
      ['assistant', 'unvouched snapshot'],
      ['streaming', 'local newer'],
    ])
    expect(store.getState().chat.lastChunkSeq).toBe(3)
    expect(store.getState().chat.lastChunkGen).toBe('g1')
  })

  it('drops a replayed chunk after switching into a mid-stream slot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(registeredSwitch(store, slotPayload(SLOT, midStream(3)), 's1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBe(3)
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'wor', seq: 3 }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'ld', seq: 4 }))
    expect(text(store.getState())).toBe('Hello world')
  })
})

describe('the replay floor is per slot across a switch', () => {
  it('switching from a slot with a higher floor does not drop the target\'s opening chunks', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    // A is deep into its turn; B is running and its snapshot stands at seq 3.
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'A-text', seq: 9 }))
    store.dispatch(switchSlot.pending('r1', OTHER))
    store.dispatch(registeredSwitch(store, slotPayload(OTHER, midStream(3), true), 'r1', OTHER))
    expect(store.getState().chat.lastChunkSeq).toBe(3)
    store.dispatch(sseChatMessage({ slot: OTHER, role: 'chunk', content: 'ld', seq: 4, batched: true, parts: [{ seq: 4, text: 'ld' }] }))
    expect(text(store.getState())).toBe('Hello world')
    // A's floor is parked on its background run entry, not lost.
    expect(store.getState().chat.slotRun[SLOT]?.lastChunkSeq).toBe(9)
  })

  it('a chunk for the target that lands mid-switch is judged against the target\'s own floor', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'A-text', seq: 9 }))
    store.dispatch(sseChatMessage({ slot: OTHER, role: 'chunk', content: 'b1', seq: 1 }))
    store.dispatch(switchSlot.pending('r1', OTHER))
    expect(store.getState().chat.lastChunkSeq).toBe(1)
    store.dispatch(sseChatMessage({ slot: OTHER, role: 'chunk', content: 'b2', seq: 2, batched: true, parts: [{ seq: 2, text: 'b2' }] }))
    expect(text(store.getState())).toContain('b2')
  })
})

describe('the replay floor follows a failed switch', () => {
  it('a rejected switch restores the origin\'s floor, not the target\'s', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'A', seq: 9 }))
    store.dispatch(sseChatMessage({ slot: OTHER, role: 'chunk', content: 'b1', seq: 1 }))
    store.dispatch(switchSlot.pending('r1', OTHER))
    expect(store.getState().chat.lastChunkSeq).toBe(1)
    store.dispatch(switchSlot.rejected(new Error('404'), 'r1', OTHER, { status: 404 } as never))
    expect(store.getState().chat.activeSlot).toBe(SLOT)
    expect(store.getState().chat.lastChunkSeq).toBe(9)
    expect(store.getState().chat.slotRun[OTHER]?.lastChunkSeq).toBe(1)
  })

})

describe('warmSlotCache.fulfilled seeds the background replay guard', () => {
  it('drops a replayed chunk on a background pane and keeps the next one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(warmSlotCache.fulfilled({ ...slotPayload(OTHER, midStream(3)), warmSeq: 1 }, 'w1', OTHER))
    expect(store.getState().chat.slotRun[OTHER]?.lastChunkSeq).toBe(3)

    store.dispatch(sseChatMessage({ slot: OTHER, role: 'chunk', content: 'wor', seq: 3 }))
    store.dispatch(sseChatMessage({ slot: OTHER, role: 'chunk', content: 'ld', seq: 4 }))
    const bg = (store.getState().chat.slotMessages[OTHER] ?? []).filter(m => m.role === 'streaming').map(m => m.content).join('')
    expect(bg).toBe('Hello world')
  })

  it('does not touch the run state of a running pane (ordered frames own it)', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(warmSlotCache.fulfilled({ ...slotPayload(OTHER, midStream(3)), warmSeq: 1 }, 'w1', OTHER))
    expect(store.getState().chat.slotRun[OTHER]?.state).toBe('idle')
  })
})
