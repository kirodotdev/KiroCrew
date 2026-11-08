import { beforeEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import chatReducer, { clearPendingPermissions, replaceMessages, requestStop, setActiveSlot, sseChatMessage } from '../store/chatSlice'
import type { ChatMessage } from '../types'

// The composer's Stop marks the turn's approval cards rejected only once the
// stop request has landed. A failed stop leaves the turn running, so its cards
// must stay answerable; a coordinator card is never the turn's.

const apiMock = vi.hoisted(() => ({ stopChatSlot: vi.fn(), stopChatSlotForce: vi.fn() }))
vi.mock('../api/client', () => ({ api: apiMock }))


const SLOT = 'a'

function setup() {
  const s = configureStore({ reducer: { chat: chatReducer }, middleware: (d) => d({ serializableCheck: false, immutableCheck: false }) })
  const perm = (id: string, registry?: string): ChatMessage => ({ role: 'permission', content: id, cls: '', meta: { approval_id: id, ...(registry ? { registry } : {}) } } as ChatMessage)
  s.dispatch(setActiveSlot(SLOT))
  s.dispatch(replaceMessages([perm('turn'), perm('coord', 'coordinator')]))
  const resolved = () => s.getState().chat.messages.filter(m => m.role === 'permission').map(m => m.meta?.resolved ?? null)
  return { s, resolved }
}

beforeEach(() => {
  apiMock.stopChatSlot.mockReset()
  apiMock.stopChatSlotForce.mockReset()
})

describe('composer Stop and approval cards', () => {
  it('leaves the cards pending when the stop fails', async () => {
    const h = setup()
    apiMock.stopChatSlot.mockRejectedValueOnce(new Error('gateway busy'))
    await h.s.dispatch(requestStop({ slotId: SLOT, force: false, rejectPending: true }))
    expect(h.resolved()).toEqual([null, null])
  })

  it('leaves the cards pending when a 2xx reply refuses the stop', async () => {
    const h = setup()
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: false, error: 'crew unreachable' })
    await h.s.dispatch(requestStop({ slotId: SLOT, force: false, rejectPending: true }))
    expect(h.resolved()).toEqual([null, null])
  })

  it('rejects the turn card and keeps the coordinator card once the stop lands', async () => {
    const h = setup()
    apiMock.stopChatSlot.mockResolvedValueOnce({ ok: true })
    await h.s.dispatch(requestStop({ slotId: SLOT, force: false, rejectPending: true }))
    expect(h.resolved()).toEqual(['rejected', null])
  })

  it('does not touch the cards on screen when the reply is for another slot or an ended turn', () => {
    const h = setup()
    h.s.dispatch(clearPendingPermissions({ slot: 'b', epoch: 0 }))
    expect(h.resolved()).toEqual([null, null])
    const epoch = h.s.getState().chat.runEpoch?.[SLOT] ?? 0
    // A new turn starts on the slot after the press.
    h.s.dispatch(sseChatMessage({ slot: SLOT, role: 'user', content: 'next', ts: 1 } as never))
    h.s.dispatch(replaceMessages([{ role: 'permission', content: 'new', cls: '', meta: { approval_id: 'new' } } as ChatMessage]))
    h.s.dispatch(clearPendingPermissions({ slot: SLOT, epoch }))
    expect(h.resolved()).toEqual([null])
  })
})
