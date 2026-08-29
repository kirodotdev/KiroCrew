import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatSlot } from '../types'

const { mockDelete, mockSlots, mockDeleteSession } = vi.hoisted(() => ({
  mockDelete: vi.fn(),
  mockSlots: vi.fn(),
  mockDeleteSession: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: { deleteChatSlot: mockDelete, chatSlots: mockSlots, deleteSession: mockDeleteSession },
}))

const chatSlice = await import('../store/chatSlice')
const dashboardSlice = await import('../store/dashboardSlice')
const { deleteSlot } = chatSlice
const chatReducer = chatSlice.default
const dashboardReducer = dashboardSlice.default

const CREATED = '2026-01-01T00:00:00Z'

const slot = (key: string, closing?: boolean): ChatSlot =>
  ({ key, messages: 0, running: false, created: CREATED, incarnation: `inc-${key}`, closing })

function store(slots: ChatSlot[] = [slot('chat-1'), slot('chat-2')]) {
  return configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    middleware: g => g({ serializableCheck: false }),
    preloadedState: {
      dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), slots, slotsLoaded: true },
    } as never,
  })
}

const closingOf = (s: ReturnType<typeof store>) => s.getState().dashboard.closingSlots
const tick = async (): Promise<void> => { await new Promise(r => setTimeout(r, 0)) }

beforeEach(() => { vi.clearAllMocks() })

describe('a surviving same-incarnation slot releases on the server terminal outcome', () => {
  it('releases once the server reports no close can still pop it', async () => {
    mockDelete.mockRejectedValue(new Error('Failed to fetch'))
    mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2', false)])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never).catch(() => undefined)
    await vi.waitFor(() => expect(closingOf(s)['chat-2']).toBeUndefined(), { timeout: 4000 })
    expect(s.getState().dashboard.slots.map(x => x.key)).toContain('chat-2')
  })

  it('keeps withholding while a close can still pop it', async () => {
    mockDelete.mockRejectedValue(new Error('Failed to fetch'))
    mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2', true)])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never).catch(() => undefined)
    await tick()
    await tick()

    expect(closingOf(s)['chat-2']).toBeDefined()
  })

  it('keeps withholding when the server says nothing about the close', async () => {
    mockDelete.mockRejectedValue(new Error('Failed to fetch'))
    mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never).catch(() => undefined)
    await tick()
    await tick()

    expect(closingOf(s)['chat-2']).toBeDefined()
  })
})
