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
    __setReactionTogglePendingForTests('slot-1', 1)
    const spy = vi.spyOn(qc, 'invalidateQueries')
    invalidateReactionsForSlot(qc, 'slot-1')
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['chat-reactions', 'slot-1']))
  })

  it('reconcileReactionsOnReconnect invalidates every observed reaction slot', () => {
    seedQuery(qc, 'slot-a')
    seedQuery(qc, 'slot-b')
    const spy = vi.spyOn(qc, 'invalidateQueries')
    reconcileReactionsOnReconnect(qc)
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'slot-a']))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'slot-b']))
  })

  it('reconcileReactionsOnReconnect defers a slot with a pending toggle but reconciles the rest', () => {
    seedQuery(qc, 'slot-a')
    seedQuery(qc, 'slot-b')
    __setReactionTogglePendingForTests('slot-a', 1)
    expect(reactionTogglePending('slot-a')).toBe(true)
    const spy = vi.spyOn(qc, 'invalidateQueries')
    reconcileReactionsOnReconnect(qc)
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['chat-reactions', 'slot-a']))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'slot-b']))
  })
})
