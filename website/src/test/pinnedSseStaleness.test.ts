/**
 * A live frame can predate the arrangement it would settle away -- and a fresh one must still land.
 *
 * Pins are optimistic, so between the click and the server's acknowledgement the server still
 * reports the old membership: a frame arriving then says "nothing pinned" about rows it does not
 * yet know are pinned. Once that window closes the same report is simply true. Both directions are
 * pinned here, because a guard keyed on frame ORDER satisfied the first and broke the second --
 * discarding a fresh post-reorder frame, so no later frame ever settled the marker.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { fetchSlots, sseSlots } from '../store/dashboardSlice'
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

function arrange() {
  localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
  markPinnedSessionOrderManual()
}

const fulfilledFetch = (payload: ChatSlot[], requestId: string) =>
  ({ type: fetchSlots.fulfilled.type, payload, meta: { requestId, arg: undefined } })
const pendingFetch = (requestId: string) =>
  ({ type: fetchSlots.pending.type, payload: undefined, meta: { requestId, arg: undefined } })

describe('the SSE settle against an optimistic pin window', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => publishPinMutationKeysInFlight([]))

  it('keeps the arrangement while the server has not acknowledged the pins', () => {
    const store = makeStore()
    store.dispatch(sseSlots([slot('a', true), slot('b', true)]))
    arrange()
    publishPinMutationKeysInFlight(['a', 'b'])

    store.dispatch(sseSlots([slot('a', false), slot('b', false)]))

    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).not.toBeNull()
  })

  it('settles on the FRESH frame that arrives once the window has closed', () => {
    // The case a frame-order guard broke: this frame postdates the reorder and is simply true,
    // so discarding it would leave the marker to refreeze whatever gets pinned next.
    const store = makeStore()
    store.dispatch(sseSlots([slot('a', true), slot('b', true)]))
    arrange()
    publishPinMutationKeysInFlight(['a'])
    publishPinMutationKeysInFlight([])

    store.dispatch(sseSlots([slot('a', false), slot('b', false)]))

    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBeNull()
  })

  it('keeps the arrangement when a refetch reply predates a pending pin', () => {
    // The in-flight record is the only guard; a pin still awaiting the
    // server was issued BEFORE it, so this reply is pre-pin and reports zero membership honestly.
    const store = makeStore()
    store.dispatch(sseSlots([slot('a', true), slot('b', true)]))
    arrange()
    publishPinMutationKeysInFlight(['a', 'b'])

    store.dispatch(pendingFetch('r9'))
    store.dispatch(fulfilledFetch([slot('a', false), slot('b', false)], 'r9'))

    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).not.toBeNull()
  })

  it('settles when no mutation was ever in flight', () => {
    const store = makeStore()
    store.dispatch(sseSlots([slot('a', true), slot('b', true)]))
    arrange()

    store.dispatch(sseSlots([slot('a', false), slot('b', false)]))

    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBeNull()
  })
})
