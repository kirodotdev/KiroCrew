import { beforeEach, describe, expect, it } from 'vitest'
import {
  PINNED_SESSION_ORDER_KEY,
  commitPinnedSessionMembership,
  commitPinnedSessionOperations,
  commitPinnedSessionSnapshot,
  movePinnedSession,
  persistPinnedSessionOrder,
  readPinnedSessionOrder,
  reconcilePinnedSessionOrder,
} from '../utils/pinnedSessionOrder'

describe('pinnedSessionOrder', () => {
  beforeEach(() => localStorage.clear())

  it('reads a valid stored string array and rejects malformed storage', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['b', 'a']))
    expect(readPinnedSessionOrder()).toEqual(['b', 'a'])

    localStorage.setItem(PINNED_SESSION_ORDER_KEY, '{bad')
    expect(readPinnedSessionOrder()).toEqual([])
  })

  it('drops stale and duplicate keys while appending newly pinned sessions naturally', () => {
    expect(reconcilePinnedSessionOrder(
      ['b', 'gone', 'b', 'a'],
      ['c', 'b', 'a', 'new'],
    )).toEqual(['b', 'a', 'c', 'new'])
  })

  it('moves a pinned session to the target position without changing membership', () => {
    expect(movePinnedSession(['a', 'b', 'c'], 'a', 'c')).toEqual(['b', 'c', 'a'])
    expect(movePinnedSession(['a', 'b', 'c'], 'c', 'a')).toEqual(['c', 'a', 'b'])
    expect(movePinnedSession(['a', 'b'], 'missing', 'a')).toEqual(['a', 'b'])
  })

  it('persists the reconciled order', () => {
    persistPinnedSessionOrder(['c', 'a'])
    expect(JSON.parse(localStorage.getItem(PINNED_SESSION_ORDER_KEY)!)).toEqual(['c', 'a'])
  })

  it('commits authoritative pin membership without disturbing survivor order', () => {
    persistPinnedSessionOrder(['a', 'b'])
    commitPinnedSessionMembership('c', true)
    expect(readPinnedSessionOrder()).toEqual(['a', 'b', 'c'])
    commitPinnedSessionMembership('b', false)
    expect(readPinnedSessionOrder()).toEqual(['a', 'c'])
  })

  it('does not apply a stale baseline after concurrent storage membership changes', () => {
    persistPinnedSessionOrder(['a', 'b'])
    const captured = readPinnedSessionOrder()
    persistPinnedSessionOrder(['a'])

    commitPinnedSessionOperations([{ key: 'c', pinned: true }], ['a', 'b'], captured)

    expect(readPinnedSessionOrder()).toEqual(['a', 'c'])
  })

  it('appends a newly pinned key instead of reviving stale stored rank', () => {
    persistPinnedSessionOrder(['new', 'a'])

    commitPinnedSessionSnapshot(['a', 'new'], ['a'], ['new'])

    expect(readPinnedSessionOrder()).toEqual(['a', 'new'])
  })

  it('preserves a concurrent reorder of a newly pinned key', () => {
    persistPinnedSessionOrder(['new', 'a', 'b'])
    const captured = readPinnedSessionOrder()
    persistPinnedSessionOrder(['a', 'new', 'b'])

    commitPinnedSessionSnapshot(['a', 'b', 'new'], ['a', 'b'], ['new'], captured)

    expect(readPinnedSessionOrder()).toEqual(['a', 'new', 'b'])
  })

  it('appends a newly pinned session after the full upgrade baseline', () => {
    expect(readPinnedSessionOrder()).toEqual([])
    commitPinnedSessionMembership('c', true, ['a', 'b'])
    expect(readPinnedSessionOrder()).toEqual(['a', 'b', 'c'])
  })
})
