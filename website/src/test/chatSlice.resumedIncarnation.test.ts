import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatSlot } from '../types'

/** THE BLOCKING FINDING -- a resumed replacement must be
 *  distinguishable from the instance that was closing.
 *
 *  The tombstone recorded the closing row's `created`, but four server paths restore
 *  `created_at` from persisted metadata on resume. So after a cross-tab close-then-resume of
 *  the same key the resumed row reported the SAME creation time, compared equal, and read as
 *  "the close is still running" -- leaving a live session hidden in that tab until a reload,
 *  with no self-recovery because the key is never omitted while the resumed row is listed.
 *
 *  Identity is now a per-live-object INCARNATION the server mints in `Slot.__init__` and no
 *  restore path reassigns. */

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
const { addSlotOptimistic } = dashboardSlice

const CREATED = '2026-01-01T00:00:00Z'
const slot = (key: string, incarnation: string): ChatSlot =>
  ({ key, messages: 0, running: false, created: CREATED, incarnation })

const store = () => {
  const s = configureStore({
    reducer: { chat: chatSlice.default, dashboard: dashboardSlice.default },
    middleware: g => g({ serializableCheck: false, immutableCheck: false }),
  })
  s.dispatch(addSlotOptimistic(slot('chat-1', 'a1')) as never)
  s.dispatch(addSlotOptimistic(slot('chat-2', 'b1')) as never)
  return s
}
const closing = (s: ReturnType<typeof store>, key: string) =>
  s.getState().dashboard.closingSlots?.[key]
const tick = () => new Promise(r => setTimeout(r, 0))

describe('a resumed replacement is revealed even when its creation time is restored', () => {
  /** THE REGRESSION: identical `created`, different incarnation. */
  it('releases the hold when the resumed row carries a new incarnation', async () => {
    mockDelete.mockResolvedValue(undefined)
    // The resume restored created_at, so ONLY the incarnation differs.
    mockSlots.mockResolvedValue([slot('chat-1', 'a1'), slot('chat-2', 'b2')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()

    expect(closing(s, 'chat-2')).toBeUndefined()
  })

  /** Control: the SAME incarnation still means the close is running, so the row must
   *  stay hidden rather than accept a turn the close would cancel. */
  it('keeps the hold while the listed row is the same incarnation', async () => {
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockResolvedValue([slot('chat-1', 'a1'), slot('chat-2', 'b1')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()

    expect(closing(s, 'chat-2')).toBeDefined()
  })

  it('records the incarnation, not the creation time, as the closing identity', () => {
    const s = store()
    s.dispatch(dashboardSlice.slotCloseStarted('chat-2') as never)

    expect(closing(s, 'chat-2')?.instance).toBe('b1')
    expect(closing(s, 'chat-2')?.instance).not.toBe(CREATED)
  })
})
