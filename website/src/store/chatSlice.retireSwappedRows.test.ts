/**
 * A committed Regenerate swap names the rows it retired on its
 * `chat_variant_switch` frame; the client drops them before the refetch that
 * frame triggers. The refetch re-seats decided rows (permissions, elicitations)
 * from the client's own copies, so without the drop a decided card of the
 * replaced turn comes back beside the new reply.
 */
import { describe, expect, it } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { refreshSlot, retireSwappedRows, setActiveSlot, switchSlot } from './chatSlice'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

const user = { role: 'user', content: 'what changed?', cls: '', ts: '2026-08-12T07:00:00.000Z', meta: { mid: 'u1' } }
// The replaced turn: an answered elicitation, an approved permission and its reply.
const oldElicitation = { role: 'elicitation', content: 'Pick a branch', cls: '', ts: '2026-08-12T07:00:01.000Z', meta: { mid: 'e1', approval_id: 'ap-e1', resolved: 'accepted' } }
const oldPermission = { role: 'permission', content: 'Run git log?', cls: '', ts: '2026-08-12T07:00:02.000Z', meta: { approval_id: 'ap-p1', resolved: 'approved' } }
const oldReply = { role: 'assistant', content: 'two files changed', cls: '', ts: '2026-08-12T07:00:03.000Z', meta: { mid: 'a1' } }
// A decided card of an earlier turn the swap did not touch.
const earlier = { role: 'permission', content: 'Read the file?', cls: '', ts: '2026-08-12T06:59:00.000Z', meta: { approval_id: 'ap-keep', resolved: 'approved' } }
// The swapped server page: the new reply carrying both versions.
const newReply = {
  role: 'assistant', content: 'three files changed', cls: '', ts: '2026-08-12T07:01:00.000Z', meta: { mid: 'a2' },
  variants: [{ content: 'two files changed' }, { content: 'three files changed' }], variant_idx: 1,
}

const detail = (messages: unknown[]) => ({ key: 'active', messages, running: false, stopping: false, hasMore: false, total: messages.length })

function seeded() {
  const store = makeStore()
  store.dispatch(setActiveSlot('active'))
  store.dispatch(switchSlot.fulfilled(detail([earlier, user, oldElicitation, oldPermission, oldReply]) as never, 'r0', 'active'))
  return store
}

const refetch = (store: ReturnType<typeof makeStore>) =>
  store.dispatch(refreshSlot.fulfilled({ ...detail([earlier, user, newReply]), refreshSeq: 1, liveFrameSeqAtDispatch: 0 } as never, 'r1', 'active'))

describe('retireSwappedRows', () => {
  it('keeps the replaced turn\'s decided cards out of the swap\'s refetch', () => {
    const store = seeded()
    store.dispatch(retireSwappedRows({ slot: 'active', mids: ['e1', 'a1'], approvalIds: ['ap-e1', 'ap-p1'] }))
    refetch(store)
    const rows = store.getState().chat.messages.map(m => m.content)
    expect(rows).not.toContain('Pick a branch')
    expect(rows).not.toContain('Run git log?')
    expect(rows).not.toContain('two files changed')
    // The swap did not retire the earlier turn's card: it stays.
    expect(rows).toContain('Read the file?')
    expect(rows).toContain('three files changed')
  })

  it('without the retire step the refetch re-seats them (the defect it prevents)', () => {
    const store = seeded()
    refetch(store)
    const rows = store.getState().chat.messages.map(m => m.content)
    expect(rows).toContain('Pick a branch')
    expect(rows).toContain('Run git log?')
  })

  it('drops the rows from a background slot\'s cached transcript too', () => {
    const store = seeded()
    store.dispatch(setActiveSlot('other'))
    store.dispatch(retireSwappedRows({ slot: 'active', mids: ['e1'], approvalIds: ['ap-e1', 'ap-p1'] }))
    const cached = (store.getState().chat.slotMessages.active ?? []).map(m => m.content)
    expect(cached).not.toContain('Pick a branch')
    expect(cached).not.toContain('Run git log?')
    expect(cached).toContain('Read the file?')
  })

  it('ignores malformed and empty id lists', () => {
    const store = seeded()
    const before = store.getState().chat.messages
    store.dispatch(retireSwappedRows({ slot: 'active', mids: 'e1', approvalIds: [1, null, ''] }))
    store.dispatch(retireSwappedRows({ slot: 'active' }))
    expect(store.getState().chat.messages).toBe(before)
  })
})
