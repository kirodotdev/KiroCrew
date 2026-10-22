/**
 * A turn the open pane did not start: a Slack thread answering on its
 * channel-born tab. The server row reads `running` from the linked session's
 * lease and, having no turn number of its own for it, names none. The pane must
 * take that row as running even though it already recorded the slot's last
 * dashboard turn as ended -- a row that names that ended turn is dropped as
 * stale, and the pane would sit idle with its composer offering Resume on a
 * reply still being written.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import chatReducer, { setActiveSlot, selectContinuable, sseChatMessage, syncSlotRunningFromServer } from './chatSlice'
import dashboardReducer from './dashboardSlice'

const SLOT = 'slack_1792600000.000100'
const GEN = 'gateway-a'
const TURN = 7

function endedTurnStore() {
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'how you doing', meta: { mid: 'u-7' } }))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'assistant', content: 'Doing well!', meta: { mid: 'a-7' } }))
  store.dispatch(sseChatMessage({ slot: SLOT, role: '_done', content: '', turn: TURN, turn_gen: GEN }))
  // The Slack thread's next question lands through the transcript refresh.
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'what next?', meta: { mid: 'u-8' } }))
  return store
}

describe('a linked turn on the open pane', () => {
  it('runs on an identity-less running row and offers no Resume', () => {
    const store = endedTurnStore()
    expect(store.getState().chat.endedTurn[SLOT]).toEqual({ gen: GEN, turn: TURN })

    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false }))

    expect(store.getState().chat.slotRunning).toBe(true)
    expect(selectContinuable(store.getState() as never)).toBe(false)
  })

  it('stays idle on a running row that names the turn it saw end', () => {
    const store = endedTurnStore()

    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false, turn: TURN, turn_gen: GEN }))

    expect(store.getState().chat.slotRunning).toBe(false)
    expect(selectContinuable(store.getState() as never)).toBe(true)
  })
})
