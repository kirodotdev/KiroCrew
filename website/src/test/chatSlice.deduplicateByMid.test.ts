import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { sseChatMessage, setActiveSlot, refreshSlot, switchSlot } from '../store/chatSlice'

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer } })
}

/** Minimal slot-detail payload for the *.fulfilled reducers. */
function slotPayload(
  key: string,
  messages: { role: string; content: string; cls?: string; ts?: string; meta?: Record<string, unknown> }[],
) {
  return { key, messages, running: false, hasMore: false, total: messages.length, queue: [], stopping: false }
}

describe('deduplicateByMid in slot-detail reducers (#5981)', () => {
  it('refreshSlot.fulfilled collapses duplicate-mid rows from a raced snapshot, keeping the last', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('weixin-slot'))

    // Simulate the #5981 race outcome: the refresh snapshot carries the same
    // assistant row twice under one server-minted mid. Only deduplicateByMid
    // collapses by mid — the merge helpers' contracts are narrower — so this
    // test fails if the deduplicateByMid call is removed from the reducer.
    store.dispatch(refreshSlot.fulfilled(slotPayload('weixin-slot', [
      { role: 'assistant', content: 'stale copy', cls: 'msg msg-a', ts: '2026-08-25T19:31:00Z', meta: { mid: 'm-dup1' } },
      { role: 'assistant', content: 'fresh copy', cls: 'msg msg-a', ts: '2026-08-25T19:31:00Z', meta: { mid: 'm-dup1' } },
    ]), 'r1', 'weixin-slot'))

    const assistants = store.getState().chat.messages.filter(m => m.role === 'assistant')
    expect(assistants.length).toBe(1)
    // The LAST occurrence (freshest merge outcome) wins.
    expect(assistants[0].content).toBe('fresh copy')
    expect(assistants[0].meta?.mid).toBe('m-dup1')
  })

  it('switchSlot.fulfilled collapses duplicate-mid rows in the fetched history, keeping the last', () => {
    const store = makeStore()
    store.dispatch(switchSlot.pending('req-1', 'weixin-slot'))
    store.dispatch(switchSlot.fulfilled(slotPayload('weixin-slot', [
      { role: 'assistant', content: 'stale copy', cls: 'msg msg-a', ts: '2026-08-25T19:31:00Z', meta: { mid: 'm-dup2' } },
      { role: 'assistant', content: 'fresh copy', cls: 'msg msg-a', ts: '2026-08-25T19:31:00Z', meta: { mid: 'm-dup2' } },
    ]), 'req-1', 'weixin-slot'))

    const assistants = store.getState().chat.messages.filter(m => m.role === 'assistant')
    expect(assistants.length).toBe(1)
    expect(assistants[0].content).toBe('fresh copy')
  })

  it('refreshSlot.fulfilled keeps distinct-mid rows intact', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('weixin-slot'))
    store.dispatch(refreshSlot.fulfilled(slotPayload('weixin-slot', [
      { role: 'assistant', content: 'First reply', cls: 'msg msg-a', ts: '2026-08-25T19:31:00Z', meta: { mid: 'm-111' } },
      { role: 'assistant', content: 'Second reply', cls: 'msg msg-a', ts: '2026-08-25T19:32:00Z', meta: { mid: 'm-222' } },
    ]), 'r1', 'weixin-slot'))

    const assistants = store.getState().chat.messages.filter(m => m.role === 'assistant')
    expect(assistants.length).toBe(2)
  })

  it('does NOT collapse distinct rows that illegitimately reuse a mid (different ts)', () => {
    // A crafted POST /api/chat body can supply meta.mid, which the backend
    // preserves rather than re-minting — so two genuinely distinct transcript
    // rows can share a mid. They are appended at different times, so identity
    // (mid+role+ts) keeps both; only the same-row race duplicate (identical
    // role AND ts) collapses.
    const store = makeStore()
    store.dispatch(setActiveSlot('weixin-slot'))
    store.dispatch(refreshSlot.fulfilled(slotPayload('weixin-slot', [
      { role: 'user', content: 'first message', cls: 'msg msg-u', ts: '2026-08-25T19:31:00Z', meta: { mid: 'm-reused' } },
      { role: 'user', content: 'second, distinct message', cls: 'msg msg-u', ts: '2026-08-25T19:33:00Z', meta: { mid: 'm-reused' } },
    ]), 'r1', 'weixin-slot'))

    const users = store.getState().chat.messages.filter(m => m.role === 'user')
    expect(users.length).toBe(2)
    expect(users.map(m => m.content)).toEqual(['first message', 'second, distinct message'])
  })
})

describe('WS redelivery guard (isRedeliveredMessage) for channel-born sessions (#5981)', () => {
  it('collapses duplicate mid entries after a channel-born session refresh race', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('weixin-slot'))

    // Simulate: WS delivers an assistant message (non-streaming channel path)
    store.dispatch(sseChatMessage({
      slot: 'weixin-slot',
      role: 'assistant',
      content: 'Hello from WeChat',
      ts: '2026-08-25T19:31:00Z',
      meta: { mid: 'm-abc123' },
    }))

    const msgs = store.getState().chat.messages
    // Should have exactly 1 assistant message
    const assistants = msgs.filter(m => m.role === 'assistant')
    expect(assistants.length).toBe(1)
    expect(assistants[0].content).toBe('Hello from WeChat')
    expect(assistants[0].meta?.mid).toBe('m-abc123')
  })

  it('isRedeliveredMessage blocks a second WS delivery of the same mid', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('weixin-slot'))

    // First delivery
    store.dispatch(sseChatMessage({
      slot: 'weixin-slot',
      role: 'assistant',
      content: 'Hello from WeChat',
      ts: '2026-08-25T19:31:00Z',
      meta: { mid: 'm-abc123' },
    }))

    // Second delivery of same mid (redelivery)
    store.dispatch(sseChatMessage({
      slot: 'weixin-slot',
      role: 'assistant',
      content: 'Hello from WeChat',
      ts: '2026-08-25T19:31:00Z',
      meta: { mid: 'm-abc123' },
    }))

    const msgs = store.getState().chat.messages
    const assistants = msgs.filter(m => m.role === 'assistant')
    expect(assistants.length).toBe(1)
  })

  it('allows distinct mid entries (different messages)', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('weixin-slot'))

    store.dispatch(sseChatMessage({
      slot: 'weixin-slot',
      role: 'assistant',
      content: 'First reply',
      ts: '2026-08-25T19:31:00Z',
      meta: { mid: 'm-111' },
    }))

    store.dispatch(sseChatMessage({
      slot: 'weixin-slot',
      role: 'assistant',
      content: 'Second reply',
      ts: '2026-08-25T19:32:00Z',
      meta: { mid: 'm-222' },
    }))

    const msgs = store.getState().chat.messages
    const assistants = msgs.filter(m => m.role === 'assistant')
    expect(assistants.length).toBe(2)
  })
})
