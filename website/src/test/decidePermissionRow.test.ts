/**
 * The chat permission card's decide (lib/decidePermissionRow), which both of
 * ChatPage's collapsed tool-group mounts route through.
 *
 * The regression this pins: a coordinator approval's id is the caller's and
 * recurs. Request A's card can still be on screen when request B takes the id
 * over (a superseded wait emits no resolution), and a decide sent by bare id
 * resolved whichever request held the id then -- B, a request nobody looked at.
 * Every decide is now bound to the request the row names.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import type { ChatMessage, Notification } from '../types'

const decideApproval = vi.fn((..._args: unknown[]) => Promise.resolve({}))
const deleteNotification = vi.fn((..._args: unknown[]) => Promise.resolve({}))
vi.mock('../api/client', () => ({
  api: {
    decideApproval: (...args: unknown[]) => decideApproval(...args),
    deleteNotification: (...args: unknown[]) => deleteNotification(...args),
    ackNotification: vi.fn(() => Promise.resolve({})),
  },
}))

import { decidePermissionRow } from '../lib/decidePermissionRow'
import { retireApprovalNote } from '../store/notificationsSlice'

const SLOT = 'chat-1'
const coordinatorRow = (instance: string, ts: string): ChatMessage => ({
  role: 'permission', content: '[subagent] shell', ts,
  meta: {
    approval_id: 'same-id', registry: 'coordinator', tool_input: 'ls',
    approval_target: { origin: 'coordinator', id: 'same-id', slot: SLOT, instance },
  },
})
const feedRow = (instance: string, ts: string): Notification => ({
  kind: 'approval', title: 'Tool approval: shell', body: '', ts, approval_id: 'same-id', approval_instance: instance, slot: SLOT,
})

function storeWith(messages: ChatMessage[], items: Notification[] = []) {
  return createTestStore({
    chat: { activeSlot: SLOT, messages, toolLog: [] } as unknown as RootState['chat'],
    notifications: { items } as RootState['notifications'],
  })
}

describe('decidePermissionRow', () => {
  beforeEach(() => { decideApproval.mockClear(); deleteNotification.mockClear() })

  it('a stale card for request A decides A, never request B that took its id', async () => {
    const rowA = coordinatorRow('inst-a', '1')
    const rowB = coordinatorRow('inst-b', '2')
    const store = storeWith([rowA, rowB], [feedRow('inst-a', '10'), feedRow('inst-b', '11')])
    await store.dispatch(decidePermissionRow(rowA.meta, SLOT, 'approved'))
    expect(decideApproval).toHaveBeenCalledTimes(1)
    expect(decideApproval).toHaveBeenCalledWith({ origin: 'coordinator', id: 'same-id', slot: SLOT, instance: 'inst-a' }, 'approve')
    const [a, b] = store.getState().chat.messages
    expect(a.meta?.resolved).toBe('approved')
    // B's card and feed row are untouched: nothing was decided for B.
    expect(b.meta?.resolved).toBeUndefined()
    expect(store.getState().notifications.retiredApprovals).toEqual({ '10': 'approve' })
  })

  it('a decision whose own frame retired the feed row first still removes that row', async () => {
    // The approval_resolved frame for this decision lands before its HTTP
    // response and retires the row; the landed decision must still DELETE it.
    const rowA = coordinatorRow('inst-a', '1')
    const store = storeWith([rowA], [feedRow('inst-a', '10')])
    decideApproval.mockImplementationOnce(async () => {
      store.dispatch(retireApprovalNote({ ts: '10', why: 'approve' }))
      return {}
    })
    await store.dispatch(decidePermissionRow(rowA.meta, SLOT, 'approved'))
    await vi.waitFor(() => expect(deleteNotification).toHaveBeenCalledWith('10'))
  })

  it('a chat runner row is decided by its slot and row mid', async () => {
    const row: ChatMessage = { role: 'permission', content: 'shell', ts: '1', meta: { approval_id: 'same-id', mid: 'mid-1', tool_input: 'ls' } }
    const store = storeWith([row], [feedRow('inst-a', '10')])
    await store.dispatch(decidePermissionRow(row.meta, SLOT, 'rejected_once'))
    expect(decideApproval).toHaveBeenCalledWith({ origin: 'native', id: 'same-id', slot: SLOT, mid: 'mid-1' }, 'reject_once')
    expect(store.getState().chat.messages[0].meta?.resolved).toBe('rejected_once')
    // A coordinator feed row under the same id is not this request.
    expect(store.getState().notifications.retiredApprovals ?? {}).toEqual({})
  })

  it('a row that names no request sends nothing and is refused as no longer pending', async () => {
    const row: ChatMessage = { role: 'permission', content: 'shell', ts: '1', meta: { approval_id: 'same-id', registry: 'coordinator' } }
    const store = storeWith([row])
    await expect(store.dispatch(decidePermissionRow(row.meta, SLOT, 'approved'))).rejects.toMatchObject({ status: 404 })
    expect(decideApproval).not.toHaveBeenCalled()
  })
})
