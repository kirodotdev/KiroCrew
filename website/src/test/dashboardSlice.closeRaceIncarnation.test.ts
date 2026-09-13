import { describe, it, expect } from 'vitest'
import dashboardReducer, { slotCloseStarted, addSlotOptimistic } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

const KEY = 'chat-9-900'

const row = (incarnation?: string): ChatSlot => ({
  key: KEY,
  title: 'Chat 9',
  messages: 0,
  running: false,
  incarnation,
} as unknown as ChatSlot)

describe('a late create for the closing instance does not revive it', () => {
  const closing = (): ReturnType<typeof dashboardReducer> => {
    let state = dashboardReducer(undefined, addSlotOptimistic(row('instance-1')))
    state = dashboardReducer(state, slotCloseStarted(KEY))
    return state
  }

  it('records the incarnation that is closing', () => {
    expect(closing().closingSlots?.[KEY]).toEqual({ instance: 'instance-1' })
  })

  it('keeps the tombstone when the create names the SAME instance', () => {
    const after = dashboardReducer(closing(), addSlotOptimistic(row('instance-1')))

    expect(after.closingSlots?.[KEY], 'tombstone released to the closing instance').toBeTruthy()
  })

  it('releases the tombstone for a genuine replacement', () => {
    const after = dashboardReducer(closing(), addSlotOptimistic(row('instance-2')))

    expect(after.closingSlots?.[KEY]).toBeUndefined()
  })
})
