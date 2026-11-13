/**
 * A pending coordinator approval stays on its chat (#28691).
 *
 * A sub-agent's tool approval is raised by the coordinator registry
 * (`interaction_coordinator.ApprovalCoordinator.request`): it is broadcast as an
 * `approval` frame and listed by `GET /api/approvals`, but it is never a row of
 * the parent slot's server transcript. The only copy of its permission row is
 * the one the socket wrote into this tab. A transcript read that rebuilds the
 * view from the server page -- switching back to the chat, or warming its
 * cache in the background -- therefore must carry that row over, and a send
 * made after the card arrived must not hide it: it stays until the registry
 * retires it (`approval_resolved`, or a reconnect that no longer lists it).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

import chatReducer, { resolveApprovalRow, selectSlotPendingApproval, sseChatMessage, switchSlot, warmSlotCache } from './chatSlice'
import { api } from '../api/client'
import { keepPendingCoordinatorApprovals } from './chat/coordinatorApprovals'
import type { ChatMessage } from '../types'

const PARENT = 'chat-parent'
const OTHER = 'chat-other'
const APPROVAL_ID = 'sub-approval-1'

const detail = vi.mocked(api.chatSlotDetail)

const ts = (s: number) => new Date(Date.UTC(2026, 10, 2, 22, 0, s)).toISOString()

/** The parent's server transcript: the user's request and the reply that
 *  started the sub-agents. No permission row: the coordinator never writes one. */
const parentPage = (extra: Array<Record<string, unknown>> = []) => ({
  key: PARENT, running: false, has_more: false, next_before: 0, queue: [], stopping: false,
  messages: [
    { role: 'user', content: 'run the backtests', ts: ts(0), meta: { mid: 'u-1' } },
    { role: 'assistant', content: 'started two sub-agents', ts: ts(1), meta: { mid: 'a-1' } },
    ...extra,
  ],
  total: 2 + extra.length,
})
const otherPage = {
  key: OTHER, running: false, has_more: false, next_before: 0, queue: [], stopping: false,
  messages: [{ role: 'user', content: 'elsewhere', ts: ts(0), meta: { mid: 'o-1' } }],
  total: 1,
}

/** The row `useWebSocket` writes for a coordinator `approval` frame. */
const approvalFrame = (slot: string, id = APPROVAL_ID) => sseChatMessage({
  slot, role: 'permission', content: '[subagent] Write File', ts: ts(5),
  meta: {
    tool_input: '{}', approval_id: id, source: 'subagent', registry: 'coordinator',
    approval_target: { origin: 'coordinator', id, slot, instance: `inst-${id}` },
  },
})

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer } })
}
type Store = ReturnType<typeof makeStore>

const pendingId = (store: Store, slot: string) =>
  (selectSlotPendingApproval(store.getState() as never, slot)?.meta as { approval_id?: string } | undefined)?.approval_id

describe('a pending coordinator approval stays on its chat (#28691)', () => {
  beforeEach(() => {
    detail.mockReset()
    detail.mockImplementation((key: string) => Promise.resolve((key === PARENT ? parentPage() : otherPage) as never))
  })

  it('survives switching away and back to the chat', async () => {
    const store = makeStore()
    await store.dispatch(switchSlot(PARENT))
    store.dispatch(approvalFrame(PARENT))
    expect(pendingId(store, PARENT)).toBe(APPROVAL_ID)

    await store.dispatch(switchSlot(OTHER))
    await store.dispatch(switchSlot(PARENT))

    expect(pendingId(store, PARENT)).toBe(APPROVAL_ID)
  })

  it('survives a background warm of the chat cache before the switch back', async () => {
    const store = makeStore()
    await store.dispatch(switchSlot(PARENT))
    store.dispatch(approvalFrame(PARENT))
    await store.dispatch(switchSlot(OTHER))

    await store.dispatch(warmSlotCache(PARENT))
    await store.dispatch(switchSlot(PARENT))

    expect(pendingId(store, PARENT)).toBe(APPROVAL_ID)
  })

  it('a chat-runner row on the page with the same id does not stand in for the card', async () => {
    // A chat-runner request id is connection-scoped and can equal a
    // coordinator id; it is a different request, so the card is kept.
    const store = makeStore()
    await store.dispatch(switchSlot(PARENT))
    store.dispatch(approvalFrame(PARENT))
    detail.mockImplementation((key: string) => Promise.resolve((key === PARENT
      ? parentPage([{ role: 'permission', content: 'Shell', ts: ts(4), meta: { approval_id: APPROVAL_ID, resolved: 'approved' } }])
      : otherPage) as never))

    await store.dispatch(switchSlot(OTHER))
    await store.dispatch(switchSlot(PARENT))

    expect(pendingId(store, PARENT)).toBe(APPROVAL_ID)
    expect((selectSlotPendingApproval(store.getState() as never, PARENT)?.meta as { registry?: string }).registry).toBe('coordinator')
  })

  it('keeps the live reply last, so a later switch still re-attaches newer reply text', async () => {
    // A card seated after the streaming reply would be the row `switchSlot`
    // reads as the live reply, and a chunk that landed during the next fetch
    // would then be replaced by the older page copy.
    const store = makeStore()
    await store.dispatch(switchSlot(PARENT))
    store.dispatch(approvalFrame(PARENT))
    const streamingPage = () => Promise.resolve({
      ...parentPage([{ role: 'streaming', content: 'A', ts: ts(7) }]), running: true,
    } as never)
    detail.mockImplementation((key: string) => (key === PARENT ? streamingPage() : Promise.resolve(otherPage as never)))
    await store.dispatch(switchSlot(OTHER))
    await store.dispatch(switchSlot(PARENT))
    const rows = store.getState().chat.messages
    expect(rows[rows.length - 1].role).toBe('streaming')
    expect(pendingId(store, PARENT)).toBe(APPROVAL_ID)

    const card = { role: 'permission', content: '[subagent] Write File', ts: ts(5), meta: { approval_id: 'x', registry: 'coordinator' } } as ChatMessage
    const reply = { role: 'assistant', content: 'done', ts: ts(8) } as ChatMessage
    const queued = { role: 'queued', content: 'next', ts: ts(9) } as ChatMessage
    expect(keepPendingCoordinatorApprovals([card], [reply, queued]).map(m => m.role)).toEqual(['permission', 'assistant', 'queued'])
  })

  it('is written once: a list that already carries the card gets no second copy', () => {
    const card = { role: 'permission', content: '[subagent] Write File', ts: ts(5), meta: { approval_id: APPROVAL_ID, registry: 'coordinator' } } as ChatMessage
    const kept = keepPendingCoordinatorApprovals([card], [card])
    expect(kept.filter(m => m.meta?.approval_id === APPROVAL_ID)).toHaveLength(1)
  })

  it.each([
    ['the open chat', true],
    ['a background chat', false],
  ])('stays the pending approval after a later user message in %s', async (_label, open) => {
    const store = makeStore()
    if (open) await store.dispatch(switchSlot(PARENT))
    store.dispatch(approvalFrame(PARENT))
    store.dispatch(sseChatMessage({ slot: PARENT, role: 'user', content: 'how is it going?', ts: ts(6), meta: { mid: 'u-2' } }))

    expect(pendingId(store, PARENT)).toBe(APPROVAL_ID)
  })

  it('leaves once the registry retires it, and a retired card is not carried over', async () => {
    const store = makeStore()
    await store.dispatch(switchSlot(PARENT))
    store.dispatch(approvalFrame(PARENT))
    store.dispatch(resolveApprovalRow({
      target: { origin: 'coordinator', id: APPROVAL_ID, slot: PARENT, instance: `inst-${APPROVAL_ID}` },
      decision: 'approved',
    }))
    expect(pendingId(store, PARENT)).toBeUndefined()

    await store.dispatch(switchSlot(OTHER))
    await store.dispatch(switchSlot(PARENT))

    expect(pendingId(store, PARENT)).toBeUndefined()
  })

  it.each([
    ['the open chat', true],
    ['a background chat', false],
  ])('a chat-runner permission row behind a later user message stays retired in %s', async (_label, open) => {
    // The turn boundary still retires a card that is not the registry's: a
    // chat-runner approval from an earlier turn cannot be answered any more.
    const store = makeStore()
    if (open) await store.dispatch(switchSlot(PARENT))
    store.dispatch(sseChatMessage({
      slot: PARENT, role: 'permission', content: 'Shell', ts: ts(5),
      meta: { tool_input: 'ls', approval_id: 'runner-1' },
    }))
    store.dispatch(sseChatMessage({ slot: PARENT, role: 'user', content: 'next', ts: ts(6), meta: { mid: 'u-2' } }))

    expect(pendingId(store, PARENT)).toBeUndefined()
  })
})
