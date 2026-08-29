import { describe, it, expect } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { slotsSnapshotApplied } from '../store/chatSlice'
import dashboardReducer, { fetchSlots, slotCloseStarted } from '../store/dashboardSlice'
import { slotsResidueListener } from '../store/slotsResidueListener'
import type { ChatMessage, ChatSlot } from '../types'
import './mockApiClient'

/** The chat slice must evict residue only on a snapshot the DASHBOARD reducer accepted.
 *
 *  `fetchSlots.fulfilled` carries the thunk's `appliedProvisional`, frozen at `getState()`
 *  time. The dashboard reducer re-derives the verdict at reduce time against `closeSeq`, which a
 *  close or an optimistic add can bump in the microtask gap. Gating eviction on the frozen flag
 *  let the dashboard refuse a list while this slice pruned from it -- and eviction is by ABSENCE,
 *  so a slot created or resumed while the read travelled lost its cached transcript, activity and
 *  pending-question state, of which only the server-persisted half ever came back. */

const slot = (key: string): ChatSlot => ({ key, messages: 0, running: false })
const msg = (content: string): ChatMessage => ({ role: 'assistant', content, cls: '' })

const seededChat = (keys: string[]) => {
  const initial = chatReducer(undefined, { type: '@@INIT' })
  return {
    ...initial,
    activeSlot: null,
    slotMessages: Object.fromEntries(keys.map(k => [k, [msg(`hi from ${k}`)]])),
    slotActivity: Object.fromEntries(keys.map(k => [k, { toolLog: [], subagents: {} }])),
    slotHistory: [...keys],
  }
}

const makeStore = (keys: string[]) =>
  configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    middleware: g => g({ serializableCheck: false, immutableCheck: false }).prepend(slotsResidueListener.middleware),
    preloadedState: { chat: seededChat(keys) } as never,
  })

describe('slots residue eviction rides the dashboard reducer final verdict', () => {
  it('keeps a background slot when a close bumps closeSeq while the read is in flight', () => {
    const store = makeStore(['chat-bg'])
    const requestId = 'read-1'

    // Issued BEFORE the close, so the reply predates a membership move.
    store.dispatch({ type: fetchSlots.pending.type, meta: { requestId } })
    store.dispatch(slotCloseStarted('chat-other'))

    // The reply omits the background slot and claims applied -- the thunk's frozen guess,
    // computed before the close could advance closeSeq.
    store.dispatch({
      type: fetchSlots.fulfilled.type,
      payload: [slot('chat-active')],
      meta: { requestId, appliedProvisional: true },
    })

    expect(store.getState().dashboard.lastSlotsRead).toEqual({ readId: requestId, applied: false })
    expect(store.getState().chat.slotMessages['chat-bg']).toBeDefined()
    expect(store.getState().chat.slotActivity['chat-bg']).toBeDefined()
    expect(store.getState().chat.slotHistory).toEqual(['chat-bg'])
  })

  it('still evicts an absent slot when the dashboard accepts the snapshot', () => {
    const store = makeStore(['chat-bg'])
    const requestId = 'read-2'
    store.dispatch({ type: fetchSlots.pending.type, meta: { requestId } })
    store.dispatch({
      type: fetchSlots.fulfilled.type,
      payload: [slot('chat-active')],
      meta: { requestId, appliedProvisional: true },
    })

    expect(store.getState().dashboard.lastSlotsRead).toEqual({ readId: requestId, applied: true })
    expect(store.getState().chat.slotMessages['chat-bg']).toBeUndefined()
    expect(store.getState().chat.slotHistory).toEqual([])
  })

  it('does not let one read verdict license eviction on a different read', () => {
    const store = makeStore(['chat-bg'])
    store.dispatch({ type: fetchSlots.pending.type, meta: { requestId: 'read-a' } })
    store.dispatch({
      type: fetchSlots.fulfilled.type,
      payload: [slot('chat-active')],
      meta: { requestId: 'read-a' },
    })
    expect(store.getState().chat.slotMessages['chat-bg']).toBeUndefined()

    const other = makeStore(['chat-bg2'])
    // An unrecorded read: the dashboard writes its own verdict, and the key must match it.
    other.dispatch({
      type: fetchSlots.fulfilled.type,
      payload: [slot('chat-active')],
      meta: { requestId: 'read-b', appliedProvisional: true },
    })
    expect(other.getState().dashboard.lastSlotsRead?.readId).toBe('read-b')
  })

  it('reconciles when handed an accepted snapshot directly', () => {
    const store = makeStore(['chat-bg'])
    store.dispatch(slotsSnapshotApplied([slot('chat-active')]))
    expect(store.getState().chat.slotMessages['chat-bg']).toBeUndefined()
  })
})

describe('a confirming snapshot settles the unknown-close notice', () => {
  const withNotice = (seen: boolean) => {
    const initial = chatReducer(undefined, { type: '@@INIT' })
    return {
      ...initial,
      activeSlot: null,
      slotsSnapshotSeen: seen,
      slotMessages: { 'chat-gone': [msg('hi')] },
      slotHistory: ['chat-gone'],
      sessionCloseFailure: { kind: 'unknown' as const, key: 'chat-gone', title: 'Gone' },
    }
  }

  /** the residue guard also gated the SETTLE, so once a live frame had
   *  been seen -- the steady state -- no accepted snapshot could ever clear the notice. It
   *  has no auto-expiry either (the shell only auto-expires the `refused` kind), so the
   *  caution became permanent and kept telling the user something already answered. */
  it('confirms the notice even after a live frame has been seen', () => {
    const next = chatReducer(withNotice(true), slotsSnapshotApplied([slot('chat-other')]))
    expect(next.sessionCloseFailure?.kind).toBe('confirmed')
  })

  it('confirms the notice before any live frame too', () => {
    const next = chatReducer(withNotice(false), slotsSnapshotApplied([slot('chat-other')]))
    expect(next.sessionCloseFailure?.kind).toBe('confirmed')
  })

  /** Settling is keyed on ABSENCE, so a snapshot that still LISTS the session answers
   *  nothing: close-failed-same-session and close-succeeded-key-reused are indistinguishable
   *  there, and clearing would drop the caution while the ambiguity stands. */
  it('keeps the notice when the snapshot still lists the session', () => {
    const next = chatReducer(withNotice(true), slotsSnapshotApplied([slot('chat-gone')]))
    expect(next.sessionCloseFailure).not.toBeNull()
  })

  /** The guard still owns EVICTION -- moving the settle out must not move this with it. */
  it('still withholds residue eviction once a live frame owns teardown', () => {
    const next = chatReducer(withNotice(true), slotsSnapshotApplied([slot('chat-other')]))
    expect(next.slotMessages['chat-gone']).toBeDefined()
    expect(next.slotHistory).toEqual(['chat-gone'])
  })
})
