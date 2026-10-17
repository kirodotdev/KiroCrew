import { configureStore } from '@reduxjs/toolkit'

/* A create that carries its own project, or opens a purpose-built chat, tells
 * the server not to make a fresh session folder; a plain new chat does not. */

const api = vi.hoisted(() => ({
  createChatSlot: vi.fn(),
  chatSlotProject: vi.fn(() => Promise.resolve({ ok: true })),
  deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
  setSlotColor: vi.fn(() => Promise.resolve({ ok: true })),
  dashboardConfig: vi.fn(() => Promise.resolve({ default_memory_mode: 'persistent' })),
}))

vi.mock('../lib/scrollQuiet', () => ({ whenScrollQuiet: () => Promise.resolve() }))
vi.mock('../api/client', () => ({ api }))

import dashboardReducer from './dashboardSlice'
import chatReducer, { createSlot } from './chatSlice'

const makeStore = () => configureStore({
  reducer: { chat: chatReducer, dashboard: dashboardReducer },
  middleware: g => g({ serializableCheck: false, immutableCheck: false }),
})

describe('createSlot session-folder opt-out', () => {
  beforeEach(() => { api.createChatSlot.mockReset() })

  it('opts out for a carried project or a purpose-built chat only', async () => {
    api.createChatSlot.mockResolvedValue({ key: 'NEW', title: 'New' })
    const store = makeStore()

    await store.dispatch(createSlot({ project: '/repo/worktree' }))
    expect(api.createChatSlot.mock.calls[0][9]).toBe(true)
    expect(api.chatSlotProject).toHaveBeenCalledWith('NEW', '/repo/worktree')

    await store.dispatch(createSlot({ sessionFolder: false }))
    expect(api.createChatSlot.mock.calls[1][9]).toBe(true)

    await store.dispatch(createSlot())
    expect(api.createChatSlot.mock.calls[2][9]).toBe(false)
    await store.dispatch(createSlot('crew-member'))
    expect(api.createChatSlot.mock.calls[3][9]).toBe(false)
  })
})
