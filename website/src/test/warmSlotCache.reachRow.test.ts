/** A background pane must re-serve the reply whose card allowed or revoked a host. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { api } from '../api/client'
import chatReducer, { SLOT_DETAIL_MAX_LIMIT, warmSlotCache } from '../store/chatSlice'
import type { ChatMessage } from '../types'

const SLOT = 'member-radar'
const rows = (): ChatMessage[] => Array.from({ length: 2000 }, (_, i) => ({
  role: 'assistant', content: `reply-${i}`, cls: '',
  ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString(),
  meta: { mid: `mid-${i}` },
}))

afterEach(() => vi.restoreAllMocks())

function setup() {
  const history = rows()
  const held = history.slice(1200)
  history[1300] = { ...history[1300], content: 're-served reply' }
  const detail = vi.spyOn(api, 'chatSlotDetail').mockImplementation(async (_slot, limit, before) => {
    const end = before ?? history.length
    const start = Math.max(0, end - (limit ?? history.length))
    return { messages: history.slice(start, end), running: false, has_more: start > 0, total: history.length, next_before: start }
  })
  const initial = chatReducer(undefined, { type: '@@INIT' })
  const store = configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...initial, activeSlot: 'front', slotMessages: { [SLOT]: held } } },
  })
  return { store, detail, held }
}

describe('warmSlotCache reachTs', () => {
  it('walks past an overlapping page to re-serve the named reply', async () => {
    const { store, detail, held } = setup()
    await store.dispatch(warmSlotCache({ key: SLOT, reachTs: held[100].ts })).unwrap()
    expect(detail.mock.calls).toEqual([
      [SLOT, SLOT_DETAIL_MAX_LIMIT],
      [SLOT, SLOT_DETAIL_MAX_LIMIT, 1500],
      [SLOT, SLOT_DETAIL_MAX_LIMIT],
    ])
    const cached = store.getState().chat.slotMessages[SLOT]
    expect(cached.find(m => m.meta?.mid === 'mid-1300')?.content).toBe('re-served reply')
    expect(cached.some(m => m.meta?.mid === 'mid-1200')).toBe(true)
    expect(cached.at(-1)?.meta?.mid).toBe('mid-1999')
    expect(new Set(cached.map(m => m.meta?.mid)).size).toBe(cached.length)
    expect(store.getState().chat.messages).toEqual([])
    expect(store.getState().chat.activeSlot).toBe('front')
  })

  it.each(['plain', 'already fetched', 'unknown', 'ambiguous'] as const)(
    'keeps the one-page warm for %s requests', async kind => {
      const { store, detail, held } = setup()
      const reachTs = kind === 'already fetched' ? held[600].ts : kind === 'unknown' ? 'unknown' : held[100].ts
      if (kind === 'ambiguous') {
        const initial = store.getState().chat
        const duplicate = held.map((m, i) => i === 101 ? { ...m, ts: reachTs } : m)
        const ambiguous = configureStore({
          reducer: { chat: chatReducer },
          preloadedState: { chat: { ...initial, slotMessages: { [SLOT]: duplicate } } },
        })
        await ambiguous.dispatch(warmSlotCache({ key: SLOT, reachTs })).unwrap()
      } else {
        await store.dispatch(warmSlotCache(kind === 'plain' ? SLOT : { key: SLOT, reachTs })).unwrap()
        expect(store.getState().chat.slotMessages[SLOT].find(m => m.meta?.mid === 'mid-1300')?.content).toBe('reply-1300')
      }
      expect(detail.mock.calls).toEqual([[SLOT, SLOT_DETAIL_MAX_LIMIT]])
    },
  )
})
