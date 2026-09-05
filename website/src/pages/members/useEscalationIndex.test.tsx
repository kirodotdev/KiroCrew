import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { MemberConversationIndex } from '../../api/client'
import type { ChatMessage } from '../../types'
import { anyEscalationPending, escalationIndexQueryKey, indexEntries, memberSlugOf, useEscalationIndex, POLL_INTERVAL_MS } from './useEscalationIndex'

vi.mock('../../api/client', () => ({ api: { memberConversation: vi.fn() } }))

type Deferred = { promise: Promise<MemberConversationIndex>; resolve: (v: MemberConversationIndex) => void; reject: (e: unknown) => void }
function deferred(): Deferred {
  let resolve!: Deferred['resolve']
  let reject!: Deferred['reject']
  const promise = new Promise<MemberConversationIndex>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function payload(...entries: Array<{ id: string; state: string }>): MemberConversationIndex {
  return { entries: entries.map(e => ({ type: 'escalation', ...e })) }
}

/** A fresh cache per test, no retries: a failed fetch settles as an error at once. */
function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}
function freshClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}
function renderIndex<P extends object>(cb: (props: P) => ReturnType<typeof useEscalationIndex>, initialProps?: P, qc = freshClient()) {
  return { qc, ...renderHook(cb, { wrapper: wrapperFor(qc), initialProps }) }
}

/**
 * Flush the fetch's promise chain AND react-query's batched notifications
 * (notifyManager schedules each flush on a timer, not a microtask; the query
 * update and the observer's listener notification are two rounds).
 */
const tick = () => new Promise<void>(res => setTimeout(res, 0))
const flush = () => act(async () => { await tick(); await tick() })

afterEach(() => { vi.useRealTimers() })

describe('useEscalationIndex', () => {
  it('fetches on mount for a member slot and exposes the entries by id', async () => {
    const fetcher = vi.fn().mockResolvedValue(payload({ id: 'e1', state: 'pending' }, { id: 'e0', state: 'answered' }))
    const { result } = renderIndex(() => useEscalationIndex('member-oncall', { fetcher }))
    expect(result.current.states).toBeNull()
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0][0]).toBe('oncall')
    expect(result.current.states?.e1.state).toBe('pending')
    expect(result.current.states?.e0.state).toBe('answered')
  })

  it('does not fetch for a non-member slot and stays null', async () => {
    const fetcher = vi.fn()
    const { result } = renderIndex(() => useEscalationIndex('chat-1-abc', { fetcher }))
    await flush()
    expect(fetcher).not.toHaveBeenCalled()
    expect(result.current.states).toBeNull()
  })

  it('a failed fetch leaves the index unavailable (null → the caller simulates)', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('offline'))
    const { result } = renderIndex(() => useEscalationIndex('member-oncall', { fetcher }))
    await flush()
    expect(result.current.states).toBeNull()
  })

  it('a remount reads the cached index at once instead of falling back to the simulation', async () => {
    const fetcher = vi.fn().mockResolvedValue(payload({ id: 'e1', state: 'pending' }))
    const qc = freshClient()
    const first = renderIndex(() => useEscalationIndex('member-oncall', { fetcher }), undefined, qc)
    await flush()
    expect(first.result.current.states?.e1.state).toBe('pending')
    first.unmount()
    // Same cache, new pane: the last known index is there on the first render.
    const second = renderIndex(() => useEscalationIndex('member-oncall', { fetcher }), undefined, qc)
    expect(second.result.current.states?.e1.state).toBe('pending')
    expect(qc.getQueryData(escalationIndexQueryKey('oncall'))).toEqual(payload({ id: 'e1', state: 'pending' }))
  })

  it('concurrent refreshes dedupe onto the request in flight', async () => {
    const first = deferred()
    const fetcher = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(payload({ id: 'e1', state: 'answered' }))
    const { result } = renderIndex(() => useEscalationIndex('member-oncall', { fetcher }))
    expect(fetcher).toHaveBeenCalledTimes(1)
    act(() => { result.current.refresh(); result.current.refresh(); result.current.refresh() })
    // Still one request on the wire.
    expect(fetcher).toHaveBeenCalledTimes(1)
    await act(async () => { first.resolve(payload({ id: 'e1', state: 'pending' })); await first.promise })
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(result.current.states?.e1.state).toBe('pending')
    // A refresh once the wire is quiet goes out.
    act(() => { result.current.refresh() })
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(result.current.states?.e1.state).toBe('answered')
  })

  it('ignores a stale response after the slot changed', async () => {
    const stale = deferred()
    const fetcher = vi.fn()
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValue(payload({ id: 'e9', state: 'pending' }))
    const { result, rerender } = renderIndex(({ slot }: { slot: string }) => useEscalationIndex(slot, { fetcher }), { slot: 'member-oncall' })
    expect(fetcher).toHaveBeenCalledTimes(1)
    const staleSignal = fetcher.mock.calls[0][1] as AbortSignal
    rerender({ slot: 'member-release' })
    await flush()
    expect(staleSignal.aborted).toBe(true)
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls[1][0]).toBe('release')
    expect(result.current.states?.e9.state).toBe('pending')
    // The first slot's answer arrives late: it must not overwrite the new slot's index.
    await act(async () => { stale.resolve(payload({ id: 'e1', state: 'answered' })); await stale.promise })
    await flush()
    expect(result.current.states?.e1).toBeUndefined()
    expect(result.current.states?.e9.state).toBe('pending')
  })

  it('refetches when the reply tick moves (a user row was added), and on a slots push', async () => {
    const fetcher = vi.fn().mockResolvedValue(payload({ id: 'e1', state: 'pending' }))
    const { rerender } = renderIndex(
      ({ tick, needsYou }: { tick: string; needsYou: boolean }) => useEscalationIndex('member-oncall', { fetcher, replyTick: tick, slotsTick: needsYou }),
      { tick: '1:m1', needsYou: true },
    )
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(1)
    // Unrelated re-render: nothing.
    rerender({ tick: '1:m1', needsYou: true })
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(1)
    // A user row landed.
    rerender({ tick: '2:u1', needsYou: true })
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(2)
    // The slots push flipped needs_you.
    rerender({ tick: '2:u1', needsYou: false })
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('polls every 20 s only while pollWhile says a card is pending', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn().mockResolvedValue(payload({ id: 'e1', state: 'pending' }))
    const { rerender } = renderIndex(
      ({ pending }: { pending: boolean }) => useEscalationIndex('member-oncall', { fetcher, pollWhile: () => pending }),
      { pending: true },
    )
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(fetcher).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS) })
    expect(fetcher).toHaveBeenCalledTimes(2)
    rerender({ pending: false })
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3) })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('the poll predicate sees the index the fetch just returned', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn()
      .mockResolvedValueOnce(payload({ id: 'e1', state: 'pending' }))
      .mockResolvedValue(payload({ id: 'e1', state: 'answered' }))
    const pollWhile = vi.fn((states: Record<string, { state: string }> | null) => states === null || states.e1?.state === 'pending')
    renderIndex(() => useEscalationIndex('member-oncall', { fetcher, pollWhile }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(fetcher).toHaveBeenCalledTimes(1)
    // Still pending per the index: one more poll goes out…
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS) })
    expect(fetcher).toHaveBeenCalledTimes(2)
    // …which answers the card, and the poll stops on its own.
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3) })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('aborts the in-flight request on unmount', () => {
    const hang = deferred()
    const fetcher = vi.fn().mockReturnValue(hang.promise)
    const { unmount } = renderIndex(() => useEscalationIndex('member-oncall', { fetcher }))
    const signal = fetcher.mock.calls[0][1] as AbortSignal
    expect(signal.aborted).toBe(false)
    unmount()
    expect(signal.aborted).toBe(true)
  })
})

describe('helpers', () => {
  it('memberSlugOf strips the member- prefix and rejects other slots', () => {
    expect(memberSlugOf('member-oncall')).toBe('oncall')
    expect(memberSlugOf('member-')).toBeUndefined()
    expect(memberSlugOf('chat-1-x')).toBeUndefined()
    expect(memberSlugOf(undefined)).toBeUndefined()
  })

  it('indexEntries keeps escalation entries with an id and skips the rest', () => {
    const out = indexEntries({
      entries: [
        { type: 'escalation', id: 'a', state: 'pending' },
        { type: 'note', id: 'b', state: 'pending' },
        { type: 'escalation', id: '', state: 'pending' },
        null as unknown as { type: string; id: string; state: string },
      ],
    })
    expect(Object.keys(out)).toEqual(['a'])
    expect(indexEntries(null)).toEqual({})
    expect(indexEntries({ entries: undefined as unknown as [] })).toEqual({})
  })

  it('anyEscalationPending reads the index when it knows the id, else the row deadline', () => {
    const row = (id: string, deadline?: string): ChatMessage => ({ role: 'escalation', content: '', cls: '', meta: { escalation_id: id, deadline } })
    const now = Date.parse('2026-01-01T12:00:00Z')
    const future = '2026-01-01T13:00:00Z'
    const past = '2026-01-01T11:00:00Z'
    expect(anyEscalationPending([row('e1', past)], null, now)).toBe(false)
    expect(anyEscalationPending([row('e1', future)], null, now)).toBe(true)
    expect(anyEscalationPending([row('e1')], null, now)).toBe(true)
    // The index wins over the row's own deadline both ways.
    expect(anyEscalationPending([row('e1', future)], { e1: { type: 'escalation', id: 'e1', state: 'answered' } }, now)).toBe(false)
    expect(anyEscalationPending([row('e1', past)], { e1: { type: 'escalation', id: 'e1', state: 'pending' } }, now)).toBe(true)
    expect(anyEscalationPending([{ role: 'user', content: 'hi', cls: '' }], null, now)).toBe(false)
  })
})
