/**
 * `deleteHistorySession` and the prompt stash: a confirmed history delete
 * clears the deleted session's stashed drafts and nothing else.
 *
 * Split out of `ChatSliceCoverageSecondPass.test.tsx` to keep that file under
 * the 2,000-line cap; it uses the same api mock and store shape.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import chatReducer, { deleteHistorySession, fetchHistory } from '../store/chatSlice'
import dashboardReducer, { addSlotOptimistic } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { ChatSlot } from '../types'
import { addStashEntry, loadPromptStash, makeStashEntry } from '../utils/promptStash'

const apiMock = vi.hoisted(() => ({
  chatSlots: vi.fn(),
  deleteChatSlot: vi.fn(),
  deleteSession: vi.fn(),
  sessions: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))

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

const slotRow = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key,
  title: key,
  messages: 0,
  running: false,
  ...extra,
} as ChatSlot)

beforeEach(() => {
  for (const fn of Object.values(apiMock)) fn.mockReset()
  apiMock.chatSlots.mockResolvedValue([])
  apiMock.deleteChatSlot.mockResolvedValue({})
  localStorage.clear()
})

describe('deleteHistorySession and the prompt stash', () => {
  it('clears the deleted dashboard history session stash, leaving its neighbors', async () => {
    expect(addStashEntry('chat-1', makeStashEntry('deleted draft', []))).toBe(true)
    expect(addStashEntry('chat-10', makeStashEntry('neighbor draft', []))).toBe(true)
    apiMock.deleteSession.mockResolvedValue({ ok: true })
    const store = makeStore()

    await store.dispatch(deleteHistorySession('dashboard_chat-1'))
    expect(loadPromptStash('chat-1')).toEqual([])
    expect(loadPromptStash('chat-10').map(entry => entry.text)).toEqual(['neighbor draft'])
  })

  // Deleting a stacked alias removes only that transcript; the canonical
  // transcript and its slot can survive, so their drafts must too.
  it('keeps the stash when a stacked alias key is deleted', async () => {
    expect(addStashEntry('chat-3-300', makeStashEntry('surviving draft', []))).toBe(true)
    apiMock.deleteSession.mockResolvedValue({ ok: true })
    const store = makeStore()

    await store.dispatch(deleteHistorySession('dashboard_dashboard_chat-3-300'))

    expect(apiMock.deleteSession).toHaveBeenCalledWith('dashboard_dashboard_chat-3-300')
    expect(loadPromptStash('chat-3-300').map(entry => entry.text)).toEqual(['surviving draft'])
  })

  // A search result can name a session that is also a live slot. The gateway
  // removes that slot as part of a confirmed history delete, so its stash must
  // be cleared too; a recreated session may immediately use the same key.
  it('clears the stash when the confirmed history delete also removes a live slot', async () => {
    expect(addStashEntry('chat-live', makeStashEntry('live draft', []))).toBe(true)
    apiMock.deleteSession.mockResolvedValue({ ok: true })
    const store = makeStore()
    store.dispatch(addSlotOptimistic(slotRow('chat-live', { mode: 'dashboard', surface: 'dashboard' })))

    await store.dispatch(deleteHistorySession('dashboard_chat-live'))

    expect(apiMock.deleteSession).toHaveBeenCalledWith('dashboard_chat-live')
    expect(loadPromptStash('chat-live')).toEqual([])
    expect(addStashEntry('chat-live', makeStashEntry('new session draft', []))).toBe(true)
    expect(loadPromptStash('chat-live').map(entry => entry.text)).toEqual(['new session draft'])
  })

  it('keeps the stash but preserves base row removal when a 200 response says ok false', async () => {
    const key = 'dashboard_chat-held'
    const slot = 'chat-held'
    apiMock.sessions.mockResolvedValue({
      sessions: [{ key, title: 'Held transcript' }, { key: 'h2' }],
      has_more: false,
    })
    apiMock.deleteSession.mockResolvedValue({ ok: false })
    expect(addStashEntry(slot, makeStashEntry('keep this draft', []))).toBe(true)
    const store = makeStore()
    await store.dispatch(fetchHistory(false))

    const action = await store.dispatch(deleteHistorySession(key))

    expect(action.meta.requestStatus).toBe('fulfilled')
    expect(chat(store).history.map(row => row.key)).toEqual(['h2'])
    expect(chat(store).undeletableHistory).toBeNull()
    expect(loadPromptStash(slot).map(entry => entry.text)).toEqual(['keep this draft'])
  })
})
