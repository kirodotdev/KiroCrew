/**
 * The close gate still asks when `hasLiveSessionWork` gains a term that
 * `closeBusyReasons` has no named reason for. Simulated by making the shared
 * predicate report live work on a slot whose fields match none of the named
 * reasons: the gate must ask, with the generic `work` line.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('../pages/chat/sessionLane', async importOriginal => {
  const actual = await importOriginal<typeof import('../pages/chat/sessionLane')>()
  return { ...actual, hasLiveSessionWork: () => true }
})

import { closeBusyReasons } from '../lib/closeBusyGate'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

const KEY = 'slot-1'

function stateWith(slot: Record<string, unknown>): RootState {
  return createTestStore({
    dashboard: { slots: [{ key: KEY, title: 'Chat 1', messages: 1, running: false, ...slot }] } as unknown as RootState['dashboard'],
    chat: { activeSlot: KEY, slotHistory: [] } as unknown as RootState['chat'],
  }).getState()
}

describe('closeBusyReasons fallback', () => {
  it('asks with the generic line for live work no named reason covers', () => {
    expect(closeBusyReasons(stateWith({}), KEY)).toEqual(['work'])
  })

  it('does not add the generic line when a named live-work reason already applies', () => {
    expect(closeBusyReasons(stateWith({ running: true }), KEY)).toEqual(['turn'])
  })
})
