/**
 * A notifications refetch that repeats what the store holds keeps its objects.
 *
 * Every (re)connect refetches the list. Rows, the rail badges and the sheet
 * read `notifications.items` by reference, so a refetch that hands back equal
 * content must leave the array, and each equal item, as it was.
 */
import { describe, it, expect } from 'vitest'
import reducer, { fetchNotifications, ackNotification } from './notificationsSlice'
import type { Notification } from '../types'

type State = ReturnType<typeof reducer>

const note = (ts: string, extra: Partial<Notification> = {}): Notification =>
  ({ ts, title: `note ${ts}`, body: 'body', acked: false, ...extra }) as Notification

/** A fresh copy each call, as a parsed HTTP reply would be. */
const reply = (items: Notification[], seq = 0, ackSeq = 0) => ({
  type: fetchNotifications.fulfilled.type,
  payload: { items: JSON.parse(JSON.stringify(items)) as Notification[], seq, ackSeq },
})

const ITEMS = [note('t1'), note('t2'), note('t3')]

const loaded = (): State => reducer(undefined, reply(ITEMS))

describe('fetchNotifications.fulfilled identity', () => {
  it('keeps the list and every item when the refetch is equal', () => {
    const before = loaded()
    const after = reducer(before, reply(ITEMS))
    expect(after.items).toBe(before.items)
    after.items.forEach((n, i) => expect(n).toBe(before.items[i]))
  })

  it('replaces only the item that changed', () => {
    const before = loaded()
    const after = reducer(before, reply([ITEMS[0], note('t2', { body: 'edited' }), ITEMS[2]]))
    expect(after.items).not.toBe(before.items)
    expect(after.items[0]).toBe(before.items[0])
    expect(after.items[1].body).toBe('edited')
    expect(after.items[2]).toBe(before.items[2])
  })

  it('takes membership and order from the reply', () => {
    const before = loaded()
    const after = reducer(before, reply([ITEMS[2], ITEMS[0], note('t4')]))
    expect(after.items.map(n => n.ts)).toEqual(['t3', 't1', 't4'])
    expect(after.items[0]).toBe(before.items[2])
    expect(after.items[1]).toBe(before.items[0])
  })

  it('keeps an item whose newer local ack the reply predates', () => {
    let state = loaded()
    // The reply below was requested before this ack (its ackSeq is 0).
    state = reducer(state, { type: ackNotification.pending.type, meta: { arg: 't2' } })
    const acked = state.items[1]
    expect(acked.acked).toBe(true)
    const after = reducer(state, reply(ITEMS))
    expect(after.items).toBe(state.items)
    expect(after.items[1]).toBe(acked)
  })
})
