/**
 * Activations that put a filed session in view open its folder path.
 *
 * A session filed in a collapsed folder (or under a collapsed parent) that
 * becomes the active one leaves the user with no visible row for the chat on
 * screen. Forks and default-folder creates already ask the sidebar to open the
 * path (`requestSlotFolderOpen`, `openFoldersOnly`). These tests pin the same
 * request for a resume from history, read from the resume answer's
 * `folder_id`, and the `onlyIfActive` guard the callers that ask after an
 * await use.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({
  api: { resumeChatSlot: vi.fn() },
}))

import chatReducer, { requestSlotFolderOpen, resumeFromHistory, setActiveSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { api } from '../api/client'

const resumeChatSlotMock = api.resumeChatSlot as unknown as ReturnType<typeof vi.fn>

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

const answer = (extra: Record<string, unknown> = {}) => ({ ok: true, key: 'chat-7-50', mode: 'orchestrator', messages: [], total: 0, ...extra })

beforeEach(() => {
  resumeChatSlotMock.mockReset()
})

describe('resume from history opens the folder of the session it opens', () => {
  it('asks the sidebar to open the folder path named by the resume answer', async () => {
    resumeChatSlotMock.mockResolvedValue(answer({ folder_id: 'f-collapsed' }))
    const store = makeStore()
    const before = store.getState().chat.revealNonce

    await store.dispatch(resumeFromHistory({ key: 'dashboard_chat-7-50', title: 'Old chat' }) as never)

    const chat = store.getState().chat
    expect(chat.activeSlot).toBe('chat-7-50')
    expect(chat.revealRequest).toEqual({ kind: 'session', target: 'chat-7-50', nonce: before + 1, openFoldersOnly: true, folderId: 'f-collapsed' })
    // The listed row carries the folder before any slots frame arrives.
    expect(store.getState().dashboard.slots.find(s => s.key === 'chat-7-50')?.folder_id).toBe('f-collapsed')
  })

  it('asks for nothing when the session is unfiled or the gateway names no folder', async () => {
    const store = makeStore()
    resumeChatSlotMock.mockResolvedValue(answer({ folder_id: '' }))
    await store.dispatch(resumeFromHistory({ key: 'dashboard_chat-7-50', title: 'Old chat' }) as never)
    expect(store.getState().chat.revealRequest).toBeNull()

    resumeChatSlotMock.mockResolvedValue(answer({ key: 'chat-8-60' }))
    await store.dispatch(resumeFromHistory({ key: 'dashboard_chat-8-60', title: 'Older chat' }) as never)
    expect(store.getState().chat.activeSlot).toBe('chat-8-60')
    expect(store.getState().chat.revealRequest).toBeNull()
  })

  it('asks for nothing when the resume lands on a surface the chat page does not show', async () => {
    resumeChatSlotMock.mockResolvedValue(answer({ mode: 'dashboard', folder_id: 'f-collapsed' }))
    const store = makeStore()
    await store.dispatch(resumeFromHistory({ key: 'dashboard_ops', title: 'Ops' }) as never)
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(store.getState().chat.revealRequest).toBeNull()
  })
})

describe('requestSlotFolderOpen with onlyIfActive', () => {
  it('opens the path only while the session is the active one', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('other'))
    store.dispatch(requestSlotFolderOpen({ key: 'new-1', folder_id: 'f1', onlyIfActive: true }))
    expect(store.getState().chat.revealRequest).toBeNull()

    store.dispatch(setActiveSlot('new-1'))
    store.dispatch(requestSlotFolderOpen({ key: 'new-1', folder_id: 'f1', onlyIfActive: true }))
    expect(store.getState().chat.revealRequest).toMatchObject({ target: 'new-1', openFoldersOnly: true, folderId: 'f1' })
  })

  it('keeps the unguarded request for callers that already switched', () => {
    const store = makeStore()
    store.dispatch(setActiveSlot('other'))
    store.dispatch(requestSlotFolderOpen({ key: 'pane-child', folder_id: 'f1' }))
    expect(store.getState().chat.revealRequest).toMatchObject({ target: 'pane-child', folderId: 'f1' })
  })
})
