import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  sseChatMessage, setActiveSlot, refreshSlot, switchSlot, warmSlotCache, snapshotChunkSeq,
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

describe('snapshotChunkSeq', () => {
  it('reads the trailing streaming row seq and nothing else', () => {
    expect(snapshotChunkSeq(midStream(7).map(m => ({ ...m, cls: m.cls ?? '' })))).toBe(7)
    expect(snapshotChunkSeq(midStream().map(m => ({ ...m, cls: m.cls ?? '' })))).toBeUndefined()
    expect(snapshotChunkSeq([{ role: 'assistant', content: 'done', cls: '', seq: 9 }])).toBeUndefined()
  })
})

describe('refreshSlot.fulfilled seeds the replay guard (reconnect path)', () => {
  it('drops a replayed chunk at or below the snapshot seq and keeps the next one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, midStream(3)), 'r1', SLOT))
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
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, midStream()), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBeUndefined()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'ld', seq: 4 }))
    expect(text(store.getState())).toBe('Hello world')
  })

  it('never lowers a floor a live frame already moved past', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Hello world!', seq: 5 }))
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, midStream(3)), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBe(5)
  })

  it('does not seed from an idle snapshot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, midStream(3), false), 'r1', SLOT))
    expect(store.getState().chat.lastChunkSeq).toBeUndefined()
  })
})

describe('seqs are the slot\'s, so the floor survives an unobserved turn boundary', () => {
  it('an idle snapshot clears the floor (a restarted gateway numbers from 0 again)', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'old turn', seq: 9 }))
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, [{ role: 'assistant', content: 'old turn', cls: 'msg msg-a' }], false), 'r1', SLOT))
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
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, midStream(9), true), 'r1', SLOT))
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
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, [{ role: 'streaming', content: 'abc', cls: 'msg msg-a', seq: 3, gen: 'g2' }], true), 'r1', SLOT))
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
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, midStream(9), true), 'r1', SLOT))
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
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, [{ role: 'streaming', content: 'abcd', cls: 'msg msg-a', seq: 4 }], true), 'r1', SLOT))
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
    store.dispatch(refreshSlot.fulfilled(slotPayload(SLOT, midStream(3), true), 'r1', SLOT))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'ld', seq: 5, batched: true, parts: [{ seq: 5, text: 'ld' }] }))
    expect(text(store.getState())).toBe('Hello wor\n[1 chunk(s) missed]\nld')
  })
})

describe('switchSlot.fulfilled seeds the replay guard', () => {
  it('drops a replayed chunk after switching into a mid-stream slot', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(switchSlot.fulfilled(slotPayload(SLOT, midStream(3)), 's1', SLOT))
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
    store.dispatch(switchSlot.fulfilled(slotPayload(OTHER, midStream(3), true), 'r1', OTHER))
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
