/**
 * The cross-consumer reaction-toggle ledger: the single source of truth for
 * in-flight toggles shared by the hook, the `reactions_changed` frame router
 * and the reconnect reconcile. These cases cover the per-slot deferral and the
 * reconnect reconcile in isolation from React.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import {
  reactionQueryKey,
  invalidateReactionsForSlot,
  reconcileReactionsOnReconnect,
  reactionTogglePending,
  beginToggle,
  endToggle,
  invalidateReactionsForSlotOn,
  __resetReactionToggleLedgerForTests,
  __setReactionTogglePendingForTests,
} from '../hooks/reactionToggleLedger'

function seedQuery(qc: QueryClient, slot: string) {
  // Observed by nothing, but present in the cache so findAll sees it.
  qc.setQueryData(reactionQueryKey(slot), [])
}

describe('reactionToggleLedger', () => {
  let qc: QueryClient
  beforeEach(() => {
    __resetReactionToggleLedgerForTests()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  })

  it('invalidateReactionsForSlot refetches when no toggle is pending', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    invalidateReactionsForSlot(qc, 'slot-1')
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'slot-1']))
  })

  it('invalidateReactionsForSlot DEFERS while a toggle is pending', () => {
    __setReactionTogglePendingForTests(qc, 'slot-1', 1)
    const spy = vi.spyOn(qc, 'invalidateQueries')
    invalidateReactionsForSlot(qc, 'slot-1')
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['chat-reactions', 'slot-1']))
  })

  it('reconcileReactionsOnReconnect invalidates every observed reaction slot', () => {
    seedQuery(qc, 'slot-a')
    seedQuery(qc, 'slot-b')
    const spy = vi.spyOn(qc, 'invalidateQueries')
    reconcileReactionsOnReconnect([qc])
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'slot-a']))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'slot-b']))
  })

  it('reconcileReactionsOnReconnect defers a slot with a pending toggle but reconciles the rest', () => {
    seedQuery(qc, 'slot-a')
    seedQuery(qc, 'slot-b')
    __setReactionTogglePendingForTests(qc, 'slot-a', 1)
    expect(reactionTogglePending(qc, 'slot-a')).toBe(true)
    const spy = vi.spyOn(qc, 'invalidateQueries')
    reconcileReactionsOnReconnect([qc])
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['chat-reactions', 'slot-a']))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'slot-b']))
  })

  it('a toggle on one client defers only that client, and its drain flushes only that client', () => {
    // An embedded app chat runs on its own QueryClient with its own copy of
    // the slot's reactions. A toggle there must not hold back the dashboard's
    // refresh, and the dashboard must not lose a refresh to the app's drain.
    const app = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const dashSpy = vi.spyOn(qc, 'invalidateQueries')
    const appSpy = vi.spyOn(app, 'invalidateQueries')
    const keysOf = (spy: typeof dashSpy) => spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    const slotKey = JSON.stringify(['chat-reactions', 'slot-1'])

    beginToggle(app, 'slot-1')
    expect(reactionTogglePending(app, 'slot-1')).toBe(true)
    expect(reactionTogglePending(qc, 'slot-1')).toBe(false)

    invalidateReactionsForSlotOn([qc, app], 'slot-1')
    expect(keysOf(dashSpy)).toContain(slotKey) // the dashboard is not held back
    expect(keysOf(appSpy)).not.toContain(slotKey) // the app client defers

    endToggle(app, 'slot-1')
    expect(keysOf(appSpy)).toContain(slotKey) // the app's drain flushes the app
    expect(keysOf(dashSpy).filter(k => k === slotKey)).toHaveLength(1) // and leaves the dashboard alone
  })

  it('reconcileReactionsOnReconnect covers every client passed, each once', () => {
    const app = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    seedQuery(qc, 'slot-a')
    seedQuery(app, 'slot-z')
    const dashSpy = vi.spyOn(qc, 'invalidateQueries')
    const appSpy = vi.spyOn(app, 'invalidateQueries')
    reconcileReactionsOnReconnect([qc, app, qc])
    expect(dashSpy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))).toEqual([JSON.stringify(['chat-reactions', 'slot-a'])])
    expect(appSpy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))).toEqual([JSON.stringify(['chat-reactions', 'slot-z'])])
  })
})
