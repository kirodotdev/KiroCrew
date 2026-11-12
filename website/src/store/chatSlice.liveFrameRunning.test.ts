/**
 * A turn started outside this tab (Slack, cron) can stream chunks and tool
 * rows before any `slots` running frame arrives. Those live frames alone must
 * mark the active chat running, so the composer shows Stop and no Resume,
 * even after an error row (#34409). The idle `slots` frame still hands Resume
 * back for a turn that died.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import chatReducer, { setActiveSlot, selectComposerBusy, selectContinuable, sseChatMessage, syncSlotRunningFromServer } from './chatSlice'
import dashboardReducer from './dashboardSlice'

const SLOT = 'chat-a'

function externalTurnStore() {
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'please summarise the repo', meta: { mid: 'u1' } }))
  return store
}

const state = (store: ReturnType<typeof externalTurnStore>) => store.getState() as never

describe('live frames of a turn this tab did not start', () => {
  it.each(['chunk', 'tool'] as const)('a %s frame offers Stop and no Resume', role => {
    const store = externalTurnStore()
    expect(selectContinuable(state(store))).toBe(true)

    store.dispatch(sseChatMessage({ slot: SLOT, role, content: 'Working', meta: { mid: `${role}-1` } }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'error', content: 'tool failed', meta: { mid: 'e1' } }))

    expect(store.getState().chat.slotRunning).toBe(true)
    expect(selectComposerBusy(state(store), SLOT)).toBe(true)
    expect(selectContinuable(state(store))).toBe(false)
  })

  it('offers Resume again once the server says the turn is idle', () => {
    const store = externalTurnStore()
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Working', seq: 1 }))
    store.dispatch(sseChatMessage({ slot: SLOT, role: 'error', content: 'tool failed', meta: { mid: 'e1' } }))

    store.dispatch(syncSlotRunningFromServer({ slot: SLOT, running: false, stopping: false }))

    expect(store.getState().chat.slotRunning).toBe(false)
    expect(selectContinuable(state(store))).toBe(true)
  })
})
