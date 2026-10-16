// Neither a refetch answering at the ring cap nor a ring of live arrivals may
// evict the approval rows this tab holds: they are its own, and a pending one
// is a request the reader can still decide. Room for them comes from the
// oldest other rows.
import { describe, it, expect } from 'vitest'
import reducer, { addNotification, approvalDecisionBegan, fetchNotifications, NOTIFICATIONS_RING_CAP } from '../store/notificationsSlice'
import type { Notification } from '../types'

const localApproval = (instance: string, minute: number) => addNotification({
  kind: 'approval', title: 'Tool approval', body: 'fs_write',
  ts: String(Date.UTC(2026, 9, 8, 10, minute, 0) / 1000),
  approval_id: `spawn:${instance}`, approval_instance: instance, slot: 'dashboard:s', _local: true,
} as Notification)

const servedAtCap = (): Notification[] => Array.from({ length: NOTIFICATIONS_RING_CAP }, (_, i) => ({
  kind: 'info', title: `n${i}`, body: '', ts: new Date(Date.UTC(2026, 9, 8, 11, 0, i)).toISOString(),
})) as Notification[]

describe('a refetch at the ring cap keeps this tab\'s pending approval rows', () => {
  it('keeps the oldest row when it is a pending local approval, within the cap', () => {
    let state = reducer(undefined, { type: '@@init' })
    state = reducer(state, localApproval('inst-1', 0))
    state = reducer(state, localApproval('inst-2', 1))
    const served = servedAtCap()
    state = reducer(state, { type: fetchNotifications.fulfilled.type, payload: { items: served, seq: state.clearSeq, ackSeq: 0 } })
    expect(state.items.filter(n => n.kind === 'approval').map(n => n.approval_instance)).toEqual(['inst-1', 'inst-2'])
    expect(state.items).toHaveLength(NOTIFICATIONS_RING_CAP)
    // The room came from the oldest served rows.
    expect(state.items.some(n => n.title === 'n0')).toBe(false)
    expect(state.items.some(n => n.title === 'n1')).toBe(false)
    expect(state.items.some(n => n.title === `n${NOTIFICATIONS_RING_CAP - 1}`)).toBe(true)
  })

  it('leaves a refetch under the cap untouched', () => {
    let state = reducer(undefined, { type: '@@init' })
    state = reducer(state, localApproval('inst-1', 0))
    const served = servedAtCap().slice(0, 10)
    state = reducer(state, { type: fetchNotifications.fulfilled.type, payload: { items: served, seq: state.clearSeq, ackSeq: 0 } })
    expect(state.items).toHaveLength(11)
    expect(state.items[0].approval_instance).toBe('inst-1')
  })
})

describe('the ring cap holds this tab\'s approval rows on every path', () => {
  const fetched = (state: ReturnType<typeof reducer>, items: Notification[]) =>
    reducer(state, { type: fetchNotifications.fulfilled.type, payload: { items, seq: state.clearSeq, ackSeq: 0 } })

  it('keeps a pending local row through a ring of live arrivals', () => {
    let state = reducer(undefined, localApproval('inst-1', 0))
    for (let k = 0; k < NOTIFICATIONS_RING_CAP; k++) {
      state = reducer(state, addNotification({ kind: 'cron', ts: `f-${k}`, title: 't', body: 'b', acked: false }))
    }
    expect(state.items.some(n => n.approval_instance === 'inst-1')).toBe(true)
    expect(state.items).toHaveLength(NOTIFICATIONS_RING_CAP)
    expect(state.items.some(n => n.ts === 'f-0')).toBe(false)
  })

  it('keeps a deciding and a pending local row through a refetch at the cap', () => {
    let state = reducer(undefined, localApproval('inst-1', 0))
    state = reducer(state, localApproval('inst-2', 1))
    state = reducer(state, approvalDecisionBegan(state.items[0].ts))
    state = fetched(state, servedAtCap())
    expect(state.items.filter(n => n.kind === 'approval')).toHaveLength(2)
    expect(state.items).toHaveLength(NOTIFICATIONS_RING_CAP)
  })

  it('counts a local row once when a served row naming the same request supersedes it', () => {
    let state = reducer(undefined, localApproval('inst-1', 0))
    const served = servedAtCap().slice(1)
    served.push({
      kind: 'approval', title: 'Tool approval', body: 'fs_write', ts: new Date(Date.UTC(2026, 9, 8, 12)).toISOString(),
      approval_id: 'spawn:inst-1', approval_instance: 'inst-1', slot: 'dashboard:s',
    })
    state = fetched(state, served)
    expect(state.items).toHaveLength(NOTIFICATIONS_RING_CAP)
    expect(state.items.filter(n => n.approval_instance === 'inst-1')).toHaveLength(1)
  })
})
