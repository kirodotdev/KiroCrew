/**
 * Streaming-row key stability across chunk dispatches (smooth-streaming regression).
 *
 * ChatPage keys virtual rows via stableMsgKey: `meta.clientTs ?? ts ?? <WeakMap
 * id minted per message OBJECT>`. Streaming and thinking messages are born with
 * no `ts`, and every chunk dispatch mutates their content — so Immer finalizes a
 * NEW object per flush. Under the WeakMap fallback that minted a NEW id (→ new
 * React key) per chunk, remounting the whole row ~60x/sec: useSmoothStream's
 * reveal cursor reset each time (text snapped in whole chunks instead of the
 * per-char reveal) and every CSS/Framer animation in the row restarted from
 * phase 0 (widget-placeholder dots flashing in unison instead of breathing on
 * their stagger).
 *
 * The reducer stamps a durable `meta.clientTs` at append. These
 * tests deliberately drive the REAL reducer and mirror ChatPage's real resolver
 * (including the WeakMap fallback), so they FAIL if the reducer stops stamping
 * the birth identity — they cannot be satisfied by the test's own resolver.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage, sseThinkingChunk, refreshSlot } from '../store/chatSlice'
import { virtualKeyFor, messageRowKey } from '../pages/chat/ChatPageMessageContent'
import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'

const SLOT = 'stream-key-slot'
const initial = reducer(undefined, { type: '@@INIT' })
const withSlot = { ...initial, activeSlot: SLOT }

// Mirror of ChatPage's stableMsgKey — clientTs → ts → WeakMap-minted id. The
// WeakMap fallback is the load-bearing part: it makes these tests fail when a
// ts-less message's object identity churns without a stamped clientTs.
function makeMsgKey() {
  let seq = 0
  const ids = new WeakMap<ChatMessage, string>()
  return (m: ChatMessage): string => {
    const explicit = (m.meta?.clientTs as string | undefined) || m.ts
    if (explicit) return explicit
    let id = ids.get(m)
    if (!id) { id = `mid-${seq++}`; ids.set(m, id) }
    return id
  }
}

const single = (m: ChatMessage, idx: number): DisplayItem => ({ kind: 'single', msg: m, idx })

const chunk = (state: ReturnType<typeof reducer>, content: string, seq: number) =>
  reducer(state, sseChatMessage({ slot: SLOT, role: 'chunk', content, seq }))

describe('streaming message identity across chunk dispatches', () => {
  it('keeps the same virtual key while chunks accumulate (active slot)', () => {
    const msgKey = makeMsgKey()
    let state = chunk(withSlot, 'Hello ', 1)
    const m1 = state.messages.find(m => m.role === 'streaming')!
    const k1 = virtualKeyFor(single(m1, 0), 0, msgKey)

    state = chunk(state, 'world', 2)
    const m2 = state.messages.find(m => m.role === 'streaming')!
    // Immer replaces the mutated message object — the identity the WeakMap
    // fallback keyed on. The stamped clientTs is what keeps the key stable.
    expect(m2).not.toBe(m1)
    const k2 = virtualKeyFor(single(m2, 0), 0, msgKey)

    expect(k2).toBe(k1)
  })

  it('keeps the same virtual key through streaming → assistant finalization', () => {
    const msgKey = makeMsgKey()
    let state = chunk(withSlot, 'Answer text', 1)
    const streamingKey = virtualKeyFor(single(state.messages.find(m => m.role === 'streaming')!, 0), 0, msgKey)

    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '', ts: '2026-08-01T22:00:00Z' }))
    const done = state.messages.find(m => m.role === 'assistant')!
    // Finalization sets a server ts, but clientTs outranks ts in the resolver,
    // so the row (and its cached height) keeps its identity.
    const doneKey = virtualKeyFor(single(done, 0), 0, msgKey)

    expect(doneKey).toBe(streamingKey)
  })

  it('keeps the same virtual key while thinking content accumulates', () => {
    const msgKey = makeMsgKey()
    let state = reducer(withSlot, sseThinkingChunk({ slot: SLOT, content: 'pondering ' }))
    const t1 = state.messages.find(m => m.role === 'thinking')!
    const k1 = virtualKeyFor(single(t1, 0), 0, msgKey)

    state = reducer(state, sseThinkingChunk({ slot: SLOT, content: 'more' }))
    const t2 = state.messages.find(m => m.role === 'thinking')!
    expect(t2).not.toBe(t1)
    const k2 = virtualKeyFor(single(t2, 0), 0, msgKey)

    expect(k2).toBe(k1)
  })

  it('keeps the same virtual key on the slot-routed (background pane) chunk path', () => {
    const msgKey = makeMsgKey()
    const bg = { ...initial, activeSlot: 'other-slot' }
    let state = reducer(bg, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'bg ', seq: 1 }))
    const m1 = state.slotMessages[SLOT]!.find(m => m.role === 'streaming')!
    const k1 = virtualKeyFor(single(m1, 0), 0, msgKey)

    state = reducer(state, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'text', seq: 2 }))
    const m2 = state.slotMessages[SLOT]!.find(m => m.role === 'streaming')!
    expect(m2).not.toBe(m1)
    const k2 = virtualKeyFor(single(m2, 0), 0, msgKey)

    expect(k2).toBe(k1)
  })

  it('mints distinct identities for distinct streaming messages', () => {
    const msgKey = makeMsgKey()
    let state = chunk(withSlot, 'first', 1)
    const first = state.messages.find(m => m.role === 'streaming')!
    const firstKey = virtualKeyFor(single(first, 0), 0, msgKey)

    // Finalize, then start a second streamed answer.
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    state = chunk(state, 'second', 2)
    const second = state.messages.find(m => m.role === 'streaming')!
    const secondKey = virtualKeyFor(single(second, 1), 1, msgKey)

    expect(secondKey).not.toBe(firstKey)
  })
})

describe('messageRowKey — inner bubble identity across finalization', () => {
  it('keeps the same key through streaming → assistant finalization (real reducer)', () => {
    let state = chunk(withSlot, 'The answer', 1)
    const streamingMsg = state.messages.find(m => m.role === 'streaming')!
    const idx = state.messages.indexOf(streamingMsg)
    const keyWhileStreaming = messageRowKey(streamingMsg, idx)

    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '', ts: '2026-08-01T23:00:00Z' }))
    const finalized = state.messages.find(m => m.role === 'assistant')!
    // Same logical message: role flipped and a server ts landed, but the bubble
    // must NOT remount — a remount here destroys useSmoothStream's drain state
    // and snaps the trailing unrevealed text into view.
    expect(messageRowKey(finalized, idx)).toBe(keyWhileStreaming)
  })

  it('still distinguishes different roles and different messages', () => {
    const a = { role: 'user', content: 'q', cls: '', ts: 't1' }
    const b = { role: 'assistant', content: 'a', cls: '', ts: 't1' }
    const c = { role: 'assistant', content: 'a2', cls: '', ts: 't2' }
    expect(messageRowKey(a, 0)).not.toBe(messageRowKey(b, 1))
    expect(messageRowKey(b, 1)).not.toBe(messageRowKey(c, 2))
  })
})

describe('virtual key stability across the chat_done full-transcript reload (refreshSlot)', () => {
  // Bug: refreshSlot fires on every chat_done and refetches the ENTIRE
  // transcript, replacing state.messages with the server copies. A message
  // STREAMED this session was born with only meta.clientTs (a minted bornKey,
  // no ts); the server copy has a real ts and NO clientTs. Since the virtual
  // key is `clientTs ?? ts`, the row's key flips bornKey → serverTs on that
  // reload, remounting the row and DROPPING its measured height in the
  // virtualizer's HeightCache — a visible scroll jump every turn (the user's
  // "reload the whole history, scroll bar keeps moving up, can't reach bottom").
  // The fix carries the client-stamped clientTs onto the reloaded message so
  // the row identity (and its cached height) is continuous.
  const detailPayload = (key: string, messages: ChatMessage[]) => ({
    key, messages, running: false, stopping: false, hasMore: false, total: messages.length,
    queue: [] as { content: string; queueId: string; ts: string }[], context: undefined,
  })

  it('keeps the streamed-then-finalized assistant row key stable across refreshSlot', () => {
    let state = chunk(withSlot, 'The answer', 1)
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '', ts: 'server-ts-1' }))
    const finalized = state.messages.find(m => m.role === 'assistant')!
    const keyBefore = messageRowKey(finalized, state.messages.indexOf(finalized))

    // chat_done → refreshSlot: server returns the full transcript. The server
    // copy carries an authoritative ts but NO clientTs.
    const server: ChatMessage[] = [{ role: 'assistant', content: 'The answer', cls: 'msg msg-a', ts: 'server-ts-1' }]
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, server), 'req-1', SLOT))

    const reloaded = state.messages.find(m => m.role === 'assistant')!
    const keyAfter = messageRowKey(reloaded, state.messages.indexOf(reloaded))
    expect(keyAfter).toBe(keyBefore)
  })

  it('keeps the row key stable across a SECOND refreshSlot (identity stays durable)', () => {
    let state = chunk(withSlot, 'Durable answer', 1)
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    const keyBefore = messageRowKey(state.messages.find(m => m.role === 'assistant')!, 0)

    const server: ChatMessage[] = [{ role: 'assistant', content: 'Durable answer', cls: 'msg msg-a', ts: 'server-ts-2' }]
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, server), 'r1', SLOT))
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, server), 'r2', SLOT))

    const keyAfter = messageRowKey(state.messages.find(m => m.role === 'assistant')!, 0)
    expect(keyAfter).toBe(keyBefore)
  })

  it('does not fabricate a clientTs on a plain server message that was never streamed here', () => {
    // A history message the user never streamed this session has no clientTs
    // in state; the reload must not invent one (its ts is already stable).
    let state = chunk(withSlot, 'streamed', 1)
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    const server: ChatMessage[] = [
      { role: 'user', content: 'old question', cls: '', ts: 'hist-1' },
      { role: 'assistant', content: 'streamed', cls: 'msg msg-a', ts: 'server-ts-3' },
    ]
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, server), 'r', SLOT))
    const hist = state.messages.find(m => m.role === 'user')!
    expect(hist.meta?.clientTs).toBeUndefined()
    expect(messageRowKey(hist, 0)).toBe('user-hist-1')
  })

  it('does NOT let an older duplicate-content row steal the freshly-streamed identity', () => {
    // Reviewers' blocker: a pre-session assistant row with the SAME content as
    // the newest streamed response must NOT receive the live clientTs (forward
    // matching would flip TWO rows' keys — the history row and the streamed
    // row — instead of zero). Newest-first pairing keeps the history row keyed
    // on its own ts and the streamed row on its clientTs.
    // Seed a history "Done" (server ts, no clientTs) via an initial reload.
    let state = reducer(withSlot, refreshSlot.fulfilled(
      detailPayload(SLOT, [{ role: 'assistant', content: 'Done', cls: 'msg msg-a', ts: 'hist-ts' }]), 'r0', SLOT))
    // Stream a NEW response with identical content, then finalize.
    state = chunk(state, 'Done', 1)
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    const assistants = state.messages.filter(m => m.role === 'assistant')
    expect(assistants).toHaveLength(2)
    const histKeyBefore = messageRowKey(assistants[0], 0)   // 'assistant-hist-ts'
    const streamKeyBefore = messageRowKey(assistants[1], 1) // 'assistant-born-...'
    expect(histKeyBefore).toBe('assistant-hist-ts')

    // chat_done reload: server returns both "Done" rows with their server ts.
    const server: ChatMessage[] = [
      { role: 'assistant', content: 'Done', cls: 'msg msg-a', ts: 'hist-ts' },
      { role: 'assistant', content: 'Done', cls: 'msg msg-a', ts: 'new-ts' },
    ]
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, server), 'r1', SLOT))
    const after = state.messages.filter(m => m.role === 'assistant')
    expect(after).toHaveLength(2)
    // History row keeps its own ts identity; streamed row keeps its clientTs.
    expect(messageRowKey(after[0], 0)).toBe(histKeyBefore)
    expect(messageRowKey(after[1], 1)).toBe(streamKeyBefore)
  })

  it('does not carry a still-streaming partial identity onto an older duplicate (reconnect mid-stream)', () => {
    // A reconnect refresh can fire while a row is still 'streaming' with partial
    // text not yet in server history. If an older row's content equals that
    // partial string, pass 2 must NOT hand the live clientTs to the older row.
    let state = reducer(withSlot, refreshSlot.fulfilled(
      detailPayload(SLOT, [{ role: 'assistant', content: 'Done', cls: 'msg msg-a', ts: 'hist-ts' }]), 'r0', SLOT))
    state = chunk(state, 'Done', 1) // still 'streaming', NOT finalized
    expect(state.messages.some(m => m.role === 'streaming')).toBe(true)

    // Reconnect reload: server history has only the old row (partial not persisted).
    const server: ChatMessage[] = [{ role: 'assistant', content: 'Done', cls: 'msg msg-a', ts: 'hist-ts' }]
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, server), 'r1', SLOT))
    const hist = state.messages.find(m => m.role === 'assistant')!
    expect(hist.meta?.clientTs).toBeUndefined()
    expect(messageRowKey(hist, 0)).toBe('assistant-hist-ts')
  })

  it('keeps distinct identities when two durable rows share the same server ts (coarse clock)', () => {
    // Two fast tool-delimited assistant segments can be stamped with the SAME
    // server ts by a coarse OS clock. A plain ts→clientTs map would collapse
    // them and hand both reloaded rows one clientTs (duplicate keys). Per-ts
    // FIFO queues must preserve each row's own identity.
    // Two streamed segments this session (distinct clientTs, no ts yet).
    let state = chunk(withSlot, 'seg one', 1)
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    state = chunk(state, 'seg two', 2)
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    // First reload: server assigns BOTH the same ts. Pass 2 pairs them by content.
    const shared: ChatMessage[] = [
      { role: 'assistant', content: 'seg one', cls: 'msg msg-a', ts: 'tick' },
      { role: 'assistant', content: 'seg two', cls: 'msg msg-a', ts: 'tick' },
    ]
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, shared), 'r1', SLOT))
    const a1 = state.messages.filter(m => m.role === 'assistant')
    const k1 = messageRowKey(a1[0], 0)
    const k2 = messageRowKey(a1[1], 1)
    expect(k1).not.toBe(k2) // distinct clientTs carried

    // Second reload: now both existing rows carry clientTs AND the shared ts,
    // so this exercises the Pass-1 ts-queue. Identities must stay distinct.
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, shared), 'r2', SLOT))
    const a2 = state.messages.filter(m => m.role === 'assistant')
    expect(messageRowKey(a2[0], 0)).toBe(k1)
    expect(messageRowKey(a2[1], 1)).toBe(k2)
    expect(messageRowKey(a2[0], 0)).not.toBe(messageRowKey(a2[1], 1))
  })

  it('does not transfer a stamped assistant identity onto an unstamped tool row sharing its ts', () => {
    // Coarse clock: an unstamped tool row can share the stamped assistant's tick
    // and precede it. Pass 1 must match role+content, not ts alone — else the
    // tool row steals the assistant's clientTs and the assistant remounts.
    let state = chunk(withSlot, 'answer', 1)
    state = reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))
    // First reload: a tool row and the assistant share ts 'tick'. Pass 2 pairs
    // the ts-less assistant stamp to the assistant by content.
    const server: ChatMessage[] = [
      { role: 'tool', content: 'ran a tool', cls: '', ts: 'tick', meta: { tool_call_id: 'tc1' } },
      { role: 'assistant', content: 'answer', cls: 'msg msg-a', ts: 'tick' },
    ]
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, server), 'r1', SLOT))
    const tool1 = state.messages.find(m => m.role === 'tool')
    const asst1 = state.messages.find(m => m.role === 'assistant')!
    expect(tool1).toBeDefined()
    expect(tool1!.meta?.clientTs).toBeUndefined()
    const asstKey = messageRowKey(asst1, 0) // 'assistant-<bornKey>'

    // Second reload exercises Pass 1 (assistant is now durable: clientTs + ts).
    state = reducer(state, refreshSlot.fulfilled(detailPayload(SLOT, server), 'r2', SLOT))
    const tool2 = state.messages.find(m => m.role === 'tool')!
    const asst2 = state.messages.find(m => m.role === 'assistant')!
    expect(tool2.meta?.clientTs).toBeUndefined()            // tool did NOT steal the stamp
    expect(messageRowKey(tool2, 0)).toBe('tool-tick')
    expect(messageRowKey(asst2, 1)).toBe(asstKey)           // assistant keeps its identity
  })
})
