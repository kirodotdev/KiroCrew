/**
 * Closing a Crew Mode conductor never lands the chat on one of its crew members:
 * the person's list does not show them, so the next active slot comes from the
 * person's own rows even when the sidebar order has no other row and the crew
 * member was the session visited last.
 */
import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { deleteSlot, switchSlot } from '../store/chatSlice'
import dashboardReducer, { addSlotOptimistic, setSidebarOrder } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { ChatSlot } from '../types'
import { crewModePreviewOn } from './crewModePreview'

const apiMock = vi.hoisted(() => ({
  chatSlotDetail: vi.fn(),
  chatSlots: vi.fn(),
  deleteChatSlot: vi.fn(),
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

const slotRow = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key,
  title: key,
  messages: 0,
  running: false,
  mode: 'dashboard',
  surface: 'dashboard',
  ...extra,
} as ChatSlot)

describe('closing a Crew Mode conductor', () => {
  it('does not land on a hidden crew member', async () => {
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    apiMock.deleteChatSlot.mockResolvedValue({})
    const store = makeStore()
    store.dispatch(addSlotOptimistic(slotRow('plain')))
    store.dispatch(addSlotOptimistic(slotRow('root', { crew_mode_root: true })))
    store.dispatch(addSlotOptimistic(slotRow('worker', { crew_root: 'root' })))
    store.dispatch(setSidebarOrder(['root']))
    await store.dispatch(switchSlot('plain'))
    await store.dispatch(switchSlot('worker'))
    await store.dispatch(switchSlot('root'))

    await store.dispatch(deleteSlot('root'))
    expect(store.getState().chat.activeSlot).toBe('plain')
  })

  it('does not land on a crew member that sits right under its root in the sidebar order, on the main chat surface', async () => {
    // The main surface lands by the sidebar's displayed order (the tab-strip rule),
    // where the worker is the next row down; the landing skips it all the same.
    apiMock.chatSlotDetail.mockResolvedValue({ messages: [], running: false })
    apiMock.deleteChatSlot.mockResolvedValue({})
    const store = makeStore()
    const chat = (key: string, extra: Partial<ChatSlot> = {}) => slotRow(key, { mode: '', surface: '', ...extra })
    store.dispatch(addSlotOptimistic(chat('root', { crew_mode_root: true })))
    store.dispatch(addSlotOptimistic(chat('worker', { crew_root: 'root' })))
    store.dispatch(addSlotOptimistic(chat('plain')))
    store.dispatch(setSidebarOrder(['root', 'worker', 'plain']))
    await store.dispatch(switchSlot('root'))

    await store.dispatch(deleteSlot('root'))
    expect(store.getState().chat.activeSlot).toBe('plain')
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
