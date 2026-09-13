import { describe, it, expect, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatSlot } from '../types'

/** Declared inline rather than via `./mockApiClient`, so the hoisted `vi.mock` is
 *  registered before `chatSlice` pulls `../api/client` into the graph. */
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
const { addSlotOptimistic } = dashboardSlice

/** A close tombstone must never outlive the close.
 *
 *  Its retirement read is BOUND by requestId, but that read can resolve having proven
 *  nothing: it is refused when a second `closeSeq` bump lands during its flight, and a
 *  refused reply shows no omission. The follow-up confirm is a FRESH, UNBOUND read, so its
 *  omission could not retire the key either, and recovery fired only on a REFUSED confirm.
 *  An APPLIED confirm therefore left `closingSlots[key]` pinned to a dead read id, and
 *  `applySlots` filtered that reusable key out of every later list until reload. */

const slot = (key: string): ChatSlot => ({ key, messages: 0, running: false })

const store = () => {
  const s = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
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

describe('the close tombstone is never left pinned to a dead read', () => {
  it('settles the tombstone when an applied confirm omits the key', async () => {
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockResolvedValue([slot('chat-1')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()

    expect(closing(s, 'chat-2')).toBeUndefined()
    expect(rows(s)).toEqual(['chat-1'])
  })

  it('rebinds instead of stranding when the confirm applies but still lists the key', async () => {
    mockDelete.mockResolvedValue(undefined)
    // The server keeps listing it, so no read can prove the pop: the key stays withheld,
    // but the tombstone must be REBOUND to a fresh read rather than pinned to a dead id.
    mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()

    const firstId = closing(s, 'chat-2')?.retireReadId
    expect(closing(s, 'chat-2')).toBeDefined()
    expect(firstId).toBeDefined()

    // Let the capped backoff fire and bind a new read.
    await new Promise(r => setTimeout(r, 400))
    await tick()

    expect(closing(s, 'chat-2')).toBeDefined()
    expect(closing(s, 'chat-2')?.retireReadId).not.toBe(firstId)
  })

  /** A read that FAILS proves nothing, so it must NOT release: releasing would reveal the
   *  still-closing row, which then accepts a turn the close cancels. The row stays withheld
   *  and the read is retried until a proof arrives -- an omission, or the key listed under a
   *  DIFFERENT instance stamp, which is a replacement rather than the close still running. */
  it('keeps the tombstone when the reads fail', async () => {
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockRejectedValue(new Error('offline'))
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()

    expect(closing(s, 'chat-2')).toBeDefined()
  })

  /** Releasing on an ATTEMPT COUNT reveals a session while its close is still running: the
   *  row returns, accepts a turn, and the close then cancels it. Only omission, a different
   *  incarnation, or a definitive refusal may release. */
  it('never releases while applied lists keep listing the key', async () => {
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()
    expect(closing(s, 'chat-2')).toBeDefined()

    for (let i = 0; i < 12; i++) {
      await new Promise(r => setTimeout(r, 700))
      await tick()
    }

    expect(closing(s, 'chat-2')).toBeDefined()
    expect(rows(s)).not.toContain('chat-2')
  })

  /** The escape hatch is a PROOF, not a clock: once a list omits the key, it releases. */
  it('releases as soon as an applied list omits the key', async () => {
    mockDelete.mockResolvedValue(undefined)
    mockSlots.mockResolvedValue([slot('chat-1'), slot('chat-2')])
    const s = store()

    await s.dispatch(deleteSlot('chat-2') as never)
    await tick()
    await tick()
    expect(closing(s, 'chat-2')).toBeDefined()

    // The server finally completes the close, so the key drops out of the list.
    mockSlots.mockResolvedValue([slot('chat-1')])
    for (let i = 0; i < 4; i++) {
      await new Promise(r => setTimeout(r, 700))
      await tick()
    }

    expect(closing(s, 'chat-2')).toBeUndefined()
  })
})
