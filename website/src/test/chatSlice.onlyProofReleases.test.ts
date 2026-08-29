import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatSlot } from '../types'

/** THE BLOCKING FINDING -- a FAILED verification must not release.
 *
 *  A lost DELETE reply routes the close to `retireCloseTombstone` as an UNKNOWN outcome,
 *  and if the verification GET then fails on an ordinary network blip the tombstone was
 *  cleared -- so the still-closing row came back, accepted a turn, and the close cancelled
 *  it. A failure is no evidence, so the row stays withheld and the read is retried. Only an
 *  applied list OMITTING the key, or a definitive refusal handled before this runs,
 *  releases it. */

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

const slot = (key: string): ChatSlot => ({ key, messages: 0, running: false })

const store = () => {
  const s = configureStore({
    reducer: { chat: chatSlice.default, dashboard: dashboardSlice.default },
    middleware: g => g({ serializableCheck: false, immutableCheck: false }),
  })
  s.dispatch(addSlotOptimistic(slot('chat-1')) as never)
  s.dispatch(addSlotOptimistic(slot('chat-2')) as never)
  return s
}
const closing = (s: ReturnType<typeof store>, key: string) =>
  s.getState().dashboard.closingSlots?.[key]
const rows = (s: ReturnType<typeof store>) => s.getState().dashboard.slots.map(x => x.key)
const tick = () => new Promise(r => setTimeout(r, 0))

describe('only a proof releases a close tombstone', () => {
  it('keeps withholding when the verification read FAILS', async () => {
    mockDelete.mockResolvedValue(undefined)
    // The retiring read still LISTS the key, so it proves nothing; every read after it
    // fails, so no proof ever arrives.
    mockSlots.mockResolvedValueOnce([slot('chat-1'), slot('chat-2')])
      .mockRejectedValue(new Error('offline'))
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()
    await new Promise(r => setTimeout(r, 400))
    await tick()

    expect(closing(s, 'chat-2')).toBeDefined()
    expect(rows(s)).not.toContain('chat-2')
  })

  it('keeps withholding when the verification read is REFUSED', async () => {
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()
    // A second close bumps closeSeq, so the in-flight verification is refused as stale.
    s.dispatch(dashboardSlice.slotCloseStarted('chat-1') as never)
    await new Promise(r => setTimeout(r, 400))
    await tick()

    expect(closing(s, 'chat-2')).toBeDefined()
  })

  /** Control: the proof still works, so withholding is not unconditional. */
  it('releases as soon as an applied list omits the key', async () => {
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockResolvedValue([slot('chat-1')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()

    expect(closing(s, 'chat-2')).toBeUndefined()
  })
})
