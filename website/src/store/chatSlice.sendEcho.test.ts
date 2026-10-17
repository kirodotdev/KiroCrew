import { configureStore } from '@reduxjs/toolkit'
import { describe, expect, it } from 'vitest'
import { expandAll } from '../utils/pasteTokens'
import chatReducer, {
  appendMessage, confirmOptimisticSend, refreshSlot, resolveOptimisticSteer, setActiveSlot,
  sseChatMessage, switchSlot,
} from './chatSlice'

const slot = 'sending-slot'
const echo = {
  slot, role: 'user', content: 'column\tcount\r\n1\t42', ts: '2026-01-01T00:00:01Z',
  meta: { sendId: 's-paste', mid: 'm-user', pastes: [{ id: 'paste-1', seq: 1, lines: 2, content: 'column\tcount\r\n1\t42' }] },
}
const receipt = { slot, sendId: echo.meta.sendId, mid: echo.meta.mid }

function setup() {
  const store = configureStore({ reducer: { chat: chatReducer } })
  store.dispatch(setActiveSlot(slot))
  return store
}

describe('dashboard send identity across echoes, receipts and navigation', () => {
  it.each([
    { background: false, clientTs: undefined },
    { background: true, clientTs: undefined },
    { background: false, clientTs: 'born-user' },
    { background: true, clientTs: 'born-user' },
  ])('keeps the rendered row identity when the echo replaces its timestamp: %j', ({ background, clientTs }) => {
    const store = setup()
    store.dispatch(appendMessage({
      role: 'user', content: echo.content, cls: '', ts: '2026-01-01T00:00:00Z',
      meta: { sendId: echo.meta.sendId, ...(clientTs ? { clientTs } : {}) },
    }))
    const before = store.getState().chat.messages[0]
    const renderKey = before.meta?.clientTs ?? before.ts
    if (background) store.dispatch(switchSlot.pending('switch-request', 'other-slot'))

    store.dispatch(sseChatMessage(echo))

    const state = store.getState().chat
    const rows = background ? state.slotMessages[slot] : state.messages
    expect(rows).toHaveLength(1)
    expect(rows[0].ts).toBe(echo.ts)
    expect(rows[0].meta?.clientTs ?? rows[0].ts).toBe(renderKey)
  })

  it.each([
    { background: false, receiptFirst: false },
    { background: false, receiptFirst: true },
    { background: true, receiptFirst: false },
    { background: true, receiptFirst: true },
  ])('reconciles a steer accepted as a new turn: %j', ({ background, receiptFirst }) => {
    const store = setup()
    store.dispatch(appendMessage({
      role: 'user', content: '[ Paste #1 ]', cls: '',
      meta: { steer: true, optimistic: true, sendId: echo.meta.sendId, pastes: echo.meta.pastes },
    }))
    if (background) {
      store.dispatch(switchSlot.pending('switch-request', 'other-slot'))
      store.dispatch(sseChatMessage({ slot: 'other-slot', role: 'user', content: 'unrelated' }))
    }
    const settle = () => store.dispatch(resolveOptimisticSteer({ slot, sendId: echo.meta.sendId, outcome: 'turn' }))
    if (receiptFirst) settle()
    store.dispatch(sseChatMessage(echo))
    store.dispatch(sseChatMessage({ slot, role: 'chunk', content: 'answer' }))
    if (!receiptFirst) settle()
    store.dispatch(sseChatMessage(echo))
    const state = store.getState().chat
    const rows = background ? state.slotMessages[slot] : state.messages
    expect(rows.map(m => m.role)).toEqual(['user', 'streaming'])
    expect(rows[0].content).toBe('[ Paste #1 ]')
    expect(rows[0].meta?.pastes).toEqual(echo.meta.pastes)
    expect(rows[0].meta?.mid).toBe(echo.meta.mid)
    expect(rows[0].meta?.steer).toBeUndefined()
    expect(rows[0].meta?.optimistic).toBeUndefined()
    expect(rows[0].meta?.sendId).toBe(echo.meta.sendId)
    if (background) expect(state.messages.map(m => m.content)).toEqual(['unrelated'])
  })

  it('does not demote an unrelated steer when a different send is echoed', () => {
    const store = setup()
    store.dispatch(appendMessage({
      role: 'user', content: echo.content, cls: '',
      meta: { steer: true, optimistic: true, sendId: 'another-steer' },
    }))
    store.dispatch(sseChatMessage(echo))
    const rows = store.getState().chat.messages
    expect(rows).toHaveLength(2)
    expect(rows[0].meta).toMatchObject({ steer: true, optimistic: true, sendId: 'another-steer' })
    expect(rows[1].meta?.mid).toBe(echo.meta.mid)
  })

  it.each([false, true])('reconciles a delayed echo past a newer steer, background: %s', (background) => {
    const store = setup()
    store.dispatch(appendMessage({
      role: 'user', content: echo.content, cls: '', meta: { sendId: echo.meta.sendId },
    }))
    store.dispatch(appendMessage({
      role: 'user', content: 'newer steer', cls: '',
      meta: { steer: true, optimistic: true, sendId: 'newer-steer' },
    }))
    if (background) store.dispatch(switchSlot.pending('switch-request', 'other-slot'))

    store.dispatch(sseChatMessage(echo))
    store.dispatch(confirmOptimisticSend(receipt))
    store.dispatch(sseChatMessage(echo))

    const state = store.getState().chat
    const rows = background ? state.slotMessages[slot] : state.messages
    expect(rows.map(m => m.content)).toEqual([echo.content, 'newer steer'])
    expect(rows[0].meta?.mid).toBe(echo.meta.mid)
    expect(rows[1].meta).toMatchObject({ steer: true, optimistic: true, sendId: 'newer-steer' })
  })

  it.each([false, true])('preserves a paste bubble with an early receipt: %s', (earlyReceipt) => {
    const store = setup()
    store.dispatch(appendMessage({
      role: 'user', content: '[ Paste #1 ]', cls: '',
      meta: { sendId: echo.meta.sendId, pastes: echo.meta.pastes },
    }))
    if (earlyReceipt) store.dispatch(confirmOptimisticSend(receipt))
    store.dispatch(sseChatMessage(echo))
    store.dispatch(sseChatMessage({ slot, role: 'chunk', content: 'answer' }))
    if (!earlyReceipt) store.dispatch(confirmOptimisticSend(receipt))
    store.dispatch(sseChatMessage(echo))
    const rows = store.getState().chat.messages
    expect(rows.map(m => m.role)).toEqual(['user', 'streaming'])
    expect(rows[0].content).toBe('[ Paste #1 ]')
    expect(rows[0].meta?.pastes).toEqual(echo.meta.pastes)
    expect(rows[0].meta?.mid).toBe(echo.meta.mid)
    expect(rows[0].meta?.optimistic).toBeUndefined()
  })

  it.each([false, true])('routes the echo and delayed receipt to the origin after switching, echo first: %s', (echoFirst) => {
    const store = setup()
    if (echoFirst) store.dispatch(sseChatMessage(echo))
    store.dispatch(switchSlot.pending('switch-request', 'other-slot'))
    store.dispatch(sseChatMessage({ slot: 'other-slot', role: 'user', content: 'unrelated' }))
    if (!echoFirst) store.dispatch(sseChatMessage(echo))
    store.dispatch(sseChatMessage({ slot, role: 'chunk', content: 'answer' }))
    store.dispatch(confirmOptimisticSend(receipt))
    store.dispatch(sseChatMessage(echo))
    expect(store.getState().chat.messages.map(m => m.content)).toEqual(['unrelated'])
    const rows = store.getState().chat.slotMessages[slot]
    expect(rows.map(m => m.role)).toEqual(['user', 'streaming'])
    expect(rows[0].meta?.mid).toBe(echo.meta.mid)
  })

  it('does not duplicate a history-hydrated row when its echo and receipt arrive late', () => {
    const store = setup()
    store.dispatch(refreshSlot.fulfilled({ key: slot, running: false, hasMore: false, total: 2, nextBefore: 0, queue: [], messages: [
      { ...echo, cls: '' },
      { role: 'assistant', content: 'finished answer', cls: '', meta: { mid: 'm-answer' } },
    ] }, 'refresh-request', slot))
    store.dispatch(sseChatMessage(echo))
    store.dispatch(confirmOptimisticSend(receipt))
    const rows = store.getState().chat.messages
    expect(rows.map(m => m.role)).toEqual(['user', 'assistant'])
    expect(expandAll(rows[0].content, echo.meta.pastes)).toBe(echo.content)
    expect(rows[1].content).toBe('finished answer')
  })

  it('keeps identical text from different sends as separate messages', () => {
    const store = setup()
    store.dispatch(sseChatMessage(echo))
    store.dispatch(sseChatMessage({ ...echo, meta: { ...echo.meta, sendId: 's-second', mid: 'm-second' } }))
    store.dispatch(confirmOptimisticSend(receipt))
    expect(store.getState().chat.messages.map(m => m.meta?.mid)).toEqual(['m-user', 'm-second'])
  })
})
