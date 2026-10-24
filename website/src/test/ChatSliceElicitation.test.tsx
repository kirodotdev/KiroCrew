/**
 * The chat slice's MCP elicitation rows: how a refresh keeps an answered card,
 * when it retires a pending one the page no longer lists, and how a resolution
 * frame writes its answer onto the row its `mid` names.
 */
import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import chatReducer, { replaceMessages, resolveByApprovalId, setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { ChatMessage } from '../types'

vi.mock('../api/client', () => ({ api: {} }))

function makeStore() {
  return configureStore({
    reducer: {
      chat: chatReducer,
      dashboard: dashboardReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

type Store = ReturnType<typeof makeStore>
const chat = (store: Store) => store.getState().chat

const msg = (over: Partial<ChatMessage> & { role: string; content: string }): ChatMessage =>
  ({ cls: '', ...over }) as ChatMessage

function detail(key: string, messages: ChatMessage[], over: Record<string, unknown> = {}) {
  return { key, messages, running: false, stopping: false, hasMore: false, total: messages.length, queue: [], context: undefined, ...over }
}

const lifecycle = (type: string, arg: string, payload: unknown) => ({
  type,
  meta: { arg, requestId: 'req-1', requestStatus: type.endsWith('fulfilled') ? 'fulfilled' : 'pending' },
  payload,
})

describe('chatSlice elicitation rows', () => {
  it('a resolved elicitation card survives a refresh whose page omits it, like a permission card', () => {
    // The server tail leaves an answered elicitation out (it is transient on
    // disk), so the client's own copy -- with its decision and answer -- is
    // re-seated into the refreshed page, keyed by the row's mid.
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([
      msg({ role: 'user', content: 'go', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-u' } }),
      msg({ role: 'elicitation', content: 'Really?', ts: '2026-01-01T00:00:01Z', meta: { approval_id: 'el-9', mid: 'm-el', resolved: 'accept', answer: { confirm: true } } }),
      msg({ role: 'assistant', content: 'done', ts: '2026-01-01T00:00:02Z', meta: { mid: 'm-a' } }),
    ]))
    let s = chat(store)
    s = chatReducer(s, lifecycle('chat/refreshSlot/fulfilled', 'A', detail('A', [
      msg({ role: 'user', content: 'go', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-u' } }),
      msg({ role: 'assistant', content: 'done', ts: '2026-01-01T00:00:02Z', meta: { mid: 'm-a' } }),
    ])))
    const card = s.messages.find(m => m.role === 'elicitation')
    expect(card?.meta?.resolved).toBe('accept')
    expect(card?.meta?.answer).toEqual({ confirm: true })
    expect(s.messages.map(m => m.role)).toEqual(['user', 'elicitation', 'assistant'])
  })

  it('a PENDING elicitation card the refreshed page no longer lists is retired as stale, not left actionable', () => {
    // The gateway keeps an open ask in its tail; its absence means it was
    // answered or expired while this tab was away.
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([
      msg({ role: 'user', content: 'go', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-u' } }),
      msg({ role: 'elicitation', content: 'Really?', ts: '2026-01-01T00:00:01Z', meta: { approval_id: 'el-8', mid: 'm-el8' } }),
    ]))
    const page = detail('A', [
      msg({ role: 'user', content: 'go', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-u' } }),
      msg({ role: 'assistant', content: 'done', ts: '2026-01-01T00:00:02Z', meta: { mid: 'm-a' } }),
    ], { liveFrameSeqAtDispatch: chat(store).liveFrameSeq ?? 0 })
    let s = chat(store)
    s = chatReducer(s, lifecycle('chat/refreshSlot/fulfilled', 'A', page))
    const card = s.messages.find(m => m.role === 'elicitation')
    expect(card?.meta?.resolved).toBe('stale')
    // A card a live frame delivered after the fetch was dispatched is newer than
    // the snapshot: its absence from the page is no evidence it ended.
    const raced = { ...chat(store), liveFrameSeq: (chat(store).liveFrameSeq ?? 0) + 1 }
    const kept = chatReducer(raced, lifecycle('chat/refreshSlot/fulfilled', 'A', page))
    expect(kept.messages.find(m => m.role === 'elicitation')?.meta?.resolved).toBeUndefined()
    // A card newer than the page's last row is past the snapshot's window, so it
    // stays actionable even with no live frame counted since dispatch.
    const store3 = makeStore()
    store3.dispatch(setActiveSlot('A'))
    store3.dispatch(replaceMessages([
      msg({ role: 'user', content: 'go', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-u' } }),
      msg({ role: 'elicitation', content: 'Later?', ts: '2026-01-01T00:00:09Z', meta: { approval_id: 'el-9', mid: 'm-el9' } }),
    ]))
    const newer = chatReducer(chat(store3), lifecycle('chat/refreshSlot/fulfilled', 'A', detail('A', [
      msg({ role: 'user', content: 'go', ts: '2026-01-01T00:00:00Z', meta: { mid: 'm-u' } }),
    ], { running: true, liveFrameSeqAtDispatch: chat(store3).liveFrameSeq ?? 0 })))
    expect(newer.messages.find(m => m.role === 'elicitation')?.meta?.resolved).toBeUndefined()
    // A card the page still lists as open stays open.
    const store2 = makeStore()
    store2.dispatch(setActiveSlot('A'))
    store2.dispatch(replaceMessages([
      msg({ role: 'elicitation', content: 'Really?', ts: '2026-01-01T00:00:01Z', meta: { approval_id: 'el-8', mid: 'm-el8' } }),
    ]))
    const s2 = chatReducer(chat(store2), lifecycle('chat/refreshSlot/fulfilled', 'A', detail('A', [
      msg({ role: 'elicitation', content: 'Really?', ts: '2026-01-01T00:00:01Z', meta: { approval_id: 'el-8', mid: 'm-el8' } }),
    ])))
    expect(s2.messages.find(m => m.role === 'elicitation')?.meta?.resolved).toBeUndefined()
  })

  it('resolveByApprovalId writes an elicitation answer onto the row the mid names', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('A'))
    store.dispatch(replaceMessages([
      msg({ role: 'elicitation', content: 'old ask', meta: { approval_id: 'el-1', mid: 'm-old' } }),
      msg({ role: 'elicitation', content: 'live ask', meta: { approval_id: 'el-1', mid: 'm-live' } }),
    ]))
    store.dispatch(resolveByApprovalId({ slot: 'A', id: 'el-1', decision: 'accept', mid: 'm-live', answer: { confirm: true } }))
    const [old, live] = chat(store).messages
    expect(live.meta?.resolved).toBe('accept')
    expect(live.meta?.answer).toEqual({ confirm: true })
    expect(old.meta?.resolved).toBeUndefined()
    expect(old.meta?.answer).toBeUndefined()
  })
})

describe('chatSlice elicitation rows outside the active view', () => {
  it('a background warm keeps the answer an elicitation card sent, not only its decision', () => {
    // A split pane answered the card and the resolution frame wrote its answer
    // into the cache before the warm's snapshot (still listing it pending) landed.
    const answered = msg({ role: 'elicitation', content: 'Really?', ts: '2026-01-01T00:00:01Z', meta: { approval_id: 'el-w', mid: 'm-elw', resolved: 'accept', answer: { confirm: true } } })
    const bare = chatReducer(undefined, { type: '@@INIT' })
    const seeded = { ...bare, slotMessages: { B: [answered] } }
    const s = chatReducer(seeded, lifecycle('chat/warmSlotCache/fulfilled', 'B', detail('B', [
      msg({ role: 'elicitation', content: 'Really?', ts: '2026-01-01T00:00:01Z', meta: { approval_id: 'el-w', mid: 'm-elw' } }),
    ])))
    const card = s.slotMessages?.B?.find(m => m.role === 'elicitation')
    expect(card?.meta?.resolved).toBe('accept')
    expect(card?.meta?.answer).toEqual({ confirm: true })
  })
})
