import { beforeEach, describe, expect, it } from 'vitest'
import {
  LEGACY_PINNED_SESSION_ORDER_KEY,
  clearLegacyPinnedSessionOrder,
  movePinnedSession,
  rankedPinnedKeys,
  readLegacyPinnedSessionOrder,
  reconcilePinnedSessionOrder,
} from '../utils/pinnedSessionOrder'

describe('pinnedSessionOrder', () => {
  beforeEach(() => localStorage.clear())

  it('ranks pinned rows by the gateway pin_rank and skips unranked or unpinned rows', () => {
    expect(rankedPinnedKeys([
      { key: 'a', pinned: true, pin_rank: 2 },
      { key: 'b', pinned: true, pin_rank: 0 },
      { key: 'c', pinned: true, pin_rank: null },
      { key: 'd', pinned: false, pin_rank: 1 },
      { key: 'e', pinned: true },
    ])).toEqual(['b', 'a'])
  })

  it('drops stale and duplicate keys while appending unranked pinned sessions naturally', () => {
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

  it('reads the legacy browser order once and clears it', () => {
    localStorage.setItem(LEGACY_PINNED_SESSION_ORDER_KEY, JSON.stringify(['b', 3, 'a']))
    expect(readLegacyPinnedSessionOrder()).toEqual(['b', 'a'])
    clearLegacyPinnedSessionOrder()
    expect(localStorage.getItem(LEGACY_PINNED_SESSION_ORDER_KEY)).toBeNull()
    expect(readLegacyPinnedSessionOrder()).toEqual([])

    localStorage.setItem(LEGACY_PINNED_SESSION_ORDER_KEY, '{bad')
    expect(readLegacyPinnedSessionOrder()).toEqual([])
  })
})
