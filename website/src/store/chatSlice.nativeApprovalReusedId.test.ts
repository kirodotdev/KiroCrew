/**
 * A runner resolution that names no row gives its decision only to the newest
 * pending runner row under its id (#34411). An ACP request id is
 * connection-scoped and recurs, so an older card under the same id is an
 * earlier request: it keeps its own decision, or reads stale when this tab
 * missed its frame.
 */
import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { resolveNativeApprovalRows, setActiveSlot, sseChatMessage } from './chatSlice'

const SLOT = 'chat-a'

const card = (content: string, s: number) => sseChatMessage({
  slot: SLOT, role: 'permission', content, ts: new Date(Date.UTC(2026, 10, 10, 22, 0, s)).toISOString(),
  meta: { approval_id: '7', tool_input: content },
})

function cards(resolvedFirst?: string) {
  const store = configureStore({ reducer: { chat: chatReducer } })
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(card('shell: ls /tmp', 1))
  if (resolvedFirst) store.dispatch(resolveNativeApprovalRows({ id: '7', slot: SLOT, decision: resolvedFirst }))
  store.dispatch(card('shell: rm -rf /tmp/build', 2))
  store.dispatch(resolveNativeApprovalRows({ id: '7', slot: SLOT, decision: 'rejected' }))
  return store.getState().chat.messages
    .filter(m => m.role === 'permission')
    .map(m => `${m.content}=${m.meta?.resolved ?? 'pending'}`)
}

describe('a reused approval id without a mid (#34411)', () => {
  it('leaves the older approved card approved', () => {
    expect(cards('approved')).toEqual(['shell: ls /tmp=approved', 'shell: rm -rf /tmp/build=rejected'])
  })

  it('retires an older card still pending here as stale, not with the newer decision', () => {
    expect(cards()).toEqual(['shell: ls /tmp=stale', 'shell: rm -rf /tmp/build=rejected'])
  })
})
