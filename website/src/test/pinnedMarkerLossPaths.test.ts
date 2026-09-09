/**
 * Two ways the manual-apply flag was lost, both from state one holder could not see.
 *
 * The in-flight pin record is per-tab and in memory: a record held
 * in one tab's memory left a sibling tab's guard blind, so a pre-acknowledgement frame arriving
 * there settled away what the first tab was still writing. And recording intent must never unwind
 * an arrangement it did not create -- overwriting '1' with '1' is a same-size write that succeeds
 * even at quota, so a failed revision advance used to delete a marker that was already there.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import { pinnedMarkerListener } from '../store/pinnedMarkerOwner'
import {
  PINNED_SESSION_ORDER_KEY,
  PINNED_SESSION_ORDER_MANUAL_KEY,
  markPinnedSessionOrderManual,
} from '../utils/pinnedSessionOrder'
import { publishPinMutationKeysInFlight } from '../utils/pinMutationsInFlight'
import type { ChatSlot } from '../types'

const slot = (key: string, pinned: boolean): ChatSlot =>
  ({ key, title: key, pinned, mode: '' }) as unknown as ChatSlot

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer },
    middleware: g => g().prepend(pinnedMarkerListener.middleware),
  })
}

/** A pin this tab has issued and the server has not yet answered. */
function pinsArePending(keys: string[]): void {
  publishPinMutationKeysInFlight(keys)
}

describe('the pending-pin window holds a zero-pin frame off', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('refuses a zero-pin frame while a pin is still pending', () => {
    const store = makeStore()
    store.dispatch(sseSlots([slot('a', true), slot('b', true)]))
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    markPinnedSessionOrderManual()
    pinsArePending(['a', 'b'])

    store.dispatch(sseSlots([slot('a', false), slot('b', false)]))

    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).not.toBeNull()
  })

})

