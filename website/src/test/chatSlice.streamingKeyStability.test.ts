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
 * The fix stamps a durable `meta.clientTs` in the reducer at append. These
 * tests deliberately drive the REAL reducer and mirror ChatPage's real resolver
 * (including the WeakMap fallback), so they FAIL if the reducer stops stamping
 * the birth identity — they cannot be satisfied by the test's own resolver.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage, sseThinkingChunk } from '../store/chatSlice'
import { virtualKeyFor, messageRowKey } from '../pages/ChatPage'
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
