/**
 * `switchSlot.pending` — the run mirrors (`slotRunning`, `slotState`,
 * `slotStopping`) describe the slot ON SCREEN from the moment `activeSlot` moves.
 *
 * `pending` moves `activeSlot` and restores the cached transcript in one
 * reducer, so every reader that shapes the view from the mirrors — the
 * transcript's fold, the composer's busy rule, the Stop affordance — sees the
 * incoming slot's rows. Left alone, the mirrors kept describing the OUTGOING
 * slot until `fulfilled` landed, and the incoming transcript was painted in the
 * previous session's running state for the whole fetch window. These tests hold
 * the fetch open on purpose: the defect lives entirely inside that window.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn(), chatSlots: vi.fn().mockResolvedValue([]) } }))

import chatReducer, { switchSlot, setSlotRunning, sseChatMessage } from './chatSlice'
import { api } from '../api/client'

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer }, middleware: (getDefault) => getDefault({ immutableCheck: false }) })
}

const detail = vi.mocked(api.chatSlotDetail)
const page = (content: string) => ({ messages: [{ role: 'user', content, ts: '2026-01-01T00:00:00.000Z' }], has_more: false, total: 1, next_before: 0 })

/** Every slot resolves at once except `held`, whose `nth` read stays open so
 *  the provisional window can be observed. Earlier reads of it settle normally,
 *  which is how a test first gets the slot cached. */
function holdOpen(held: string, nth = 1): () => void {
  let release!: (v: unknown) => void
  let reads = 0
  detail.mockImplementation((key: string) => {
    if (key === held && ++reads === nth) return new Promise((resolve) => { release = resolve })
    return Promise.resolve(page(key))
  })
  return () => release(page(held))
}

describe('switchSlot.pending — run mirrors follow the slot on screen', () => {
  beforeEach(() => vi.clearAllMocks())

  it('leaving a running slot for a cached idle one reads idle at once', async () => {
    const release = holdOpen('B', 2)
    const store = makeStore()
    await store.dispatch(switchSlot('B'))          // B cached, settled, idle
    await store.dispatch(switchSlot('A'))
    store.dispatch(setSlotRunning(true))            // A is running on screen
    const inflight = store.dispatch(switchSlot('B'))
    const s = store.getState().chat
    expect(s.activeSlot).toBe('B')
    expect(s.slotLoading).toBe(false)               // the cached transcript is on screen ...
    expect(s.slotRunning).toBe(false)               // ... and so is B's own run state
    expect(s.slotState).toBe('idle')
    release(); await inflight
  })

  it('leaving an idle slot for one streaming in the background reads running at once', async () => {
    const release = holdOpen('B', 2)
    const store = makeStore()
    await store.dispatch(switchSlot('B'))
    await store.dispatch(switchSlot('A'))
    // B's live frame lands while A is on screen: its keyed entry goes busy.
    store.dispatch(sseChatMessage({ slot: 'B', role: 'chunk', content: 'x' }))
    const inflight = store.dispatch(switchSlot('B'))
    const s = store.getState().chat
    expect(s.activeSlot).toBe('B')
    expect(s.slotRunning).toBe(true)
    expect(s.slotState).toBe('streaming')
    release(); await inflight
  })

  it('a slot never seen before reads idle, not the outgoing slot\'s state', async () => {
    const release = holdOpen('fresh')
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    store.dispatch(setSlotRunning(true))
    const inflight = store.dispatch(switchSlot('fresh'))
    expect(store.getState().chat.slotRunning).toBe(false)
    release(); await inflight
  })

  it('a same-key switch leaves the mirrors alone', async () => {
    const release = holdOpen('A', 2)
    const store = makeStore()
    await store.dispatch(switchSlot('A'))
    store.dispatch(setSlotRunning(true))
    const inflight = store.dispatch(switchSlot('A'))
    // Re-selecting the slot on screen is a refresh of it, not a change of it.
    expect(store.getState().chat.slotRunning).toBe(true)
    release(); await inflight
  })

  it('fulfilled still takes the server\'s answer over the seeded mirror', async () => {
    detail.mockImplementation((key: string) => Promise.resolve({ ...page(key), running: key === 'B' }))
    const store = makeStore()
    await store.dispatch(switchSlot('B'))
    await store.dispatch(switchSlot('A'))
    await store.dispatch(switchSlot('B'))
    // B's keyed entry was idle (no frame ever reached it), so pending seeded
    // idle; the fetch says running, and the fetch wins once it lands.
    expect(store.getState().chat.slotRunning).toBe(true)
  })
})
