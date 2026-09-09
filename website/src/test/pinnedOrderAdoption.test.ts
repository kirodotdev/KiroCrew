/**
 * The one-shot upgrade adoption: an order stored before this release counts as a stated arrangement.
 *
 * Rank became opt-in here, so a user who had dragged their pinned rows holds the order but no marker,
 * and the arrangement they curated would stop applying with nothing offering it back. The cases that
 * must NOT adopt are what make it safe -- above all a second load, because ending an arrangement
 * REMOVES the marker, so absence alone cannot tell a fresh user from one who chose to follow the sort.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  PINNED_SESSION_ORDER_ADOPTED_KEY,
  PINNED_SESSION_ORDER_KEY,
  PINNED_SESSION_ORDER_MANUAL_KEY,
  adoptStoredPinnedOrderAsManual,
  forgetPinnedSessionOrderManual,
  markPinnedSessionOrderManual,
  readPinnedSessionOrder,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'

describe('adopting an order stored before rank became opt-in', () => {
  beforeEach(() => localStorage.clear())

  it('adopts a stored order that carries no marker, and preserves the order', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b', 'c']))

    expect(adoptStoredPinnedOrderAsManual()).toBe(true)

    expect(readPinnedSessionOrderIsManual()).toBe(true)
    expect(readPinnedSessionOrder()).toEqual(['a', 'b', 'c'])
  })

  it('leaves a user who never pinned anything alone', () => {
    expect(adoptStoredPinnedOrderAsManual()).toBe(false)
    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBeNull()
  })

  it('does not re-adopt on a later load after the user opted out', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    adoptStoredPinnedOrderAsManual()
    forgetPinnedSessionOrderManual()

    // The reload. The stored order survives an opt-out, so only the adopted flag can tell that this
    // browser has already been asked -- the marker itself is gone.
    expect(adoptStoredPinnedOrderAsManual()).toBe(false)

    expect(readPinnedSessionOrderIsManual()).toBe(false)
  })

  it('does not disturb an arrangement already stated', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    markPinnedSessionOrderManual()

    expect(adoptStoredPinnedOrderAsManual()).toBe(false)

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })

  it('settles the question even when nothing is adopted', () => {
    adoptStoredPinnedOrderAsManual()
    expect(localStorage.getItem(PINNED_SESSION_ORDER_ADOPTED_KEY)).toBe('1')
  })
})
