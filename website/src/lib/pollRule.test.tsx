import { QueryClient, QueryClientProvider, QueryObserver, useQuery } from '@tanstack/react-query'
import { act, render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { pollGapMs, requestRefresh, resetPollRule, timedPoll, usePollRule } from './pollRule'

const setVisibility = (state: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => { vi.useFakeTimers(); resetPollRule() })
afterEach(() => { vi.useRealTimers(); setVisibility('visible') })

describe('pollGapMs', () => {
  it('stays at the base for fast answers and doubles past a slow one', async () => {
    await timedPoll('a', async () => { vi.advanceTimersByTime(25_000) })
    expect(pollGapMs('a', 10_000)).toBe(40_000)
    await timedPoll('a', async () => 1)
    expect(pollGapMs('a', 10_000)).toBe(10_000)
  })

  it('doubles per consecutive 503 and resets on success', async () => {
    const e503 = Object.assign(new Error('busy'), { status: 503 })
    await expect(timedPoll('b', () => Promise.reject(e503))).rejects.toBe(e503)
    await expect(timedPoll('b', () => Promise.reject(e503))).rejects.toBe(e503)
    expect(pollGapMs('b', 10_000)).toBe(40_000)
    await timedPoll('b', async () => 1)
    expect(pollGapMs('b', 10_000)).toBe(10_000)
  })
})

describe('usePollRule', () => {
  const key = ['k']
  function Probe({ selected, read }: { selected: boolean; read: () => Promise<number> }) {
    const refetchInterval = usePollRule('k', 10_000, selected, key)
    useQuery({ queryKey: key, queryFn: read, refetchInterval, staleTime: Infinity })
    return null
  }
  const wrap = (qc: QueryClient, ui: ReactNode) => <QueryClientProvider client={qc}>{ui}</QueryClientProvider>

  it('polls only while selected and visible, then re-reads once', async () => {
    const qc = new QueryClient()
    const read = vi.fn(async () => 1)
    const { rerender } = render(wrap(qc, <Probe selected read={read} />))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(read).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(read).toHaveBeenCalledTimes(2)

    rerender(wrap(qc, <Probe selected={false} read={read} />))
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(read).toHaveBeenCalledTimes(2)
    rerender(wrap(qc, <Probe selected read={read} />))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(read).toHaveBeenCalledTimes(3)

    act(() => setVisibility('hidden'))
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(read).toHaveBeenCalledTimes(3)
    act(() => setVisibility('visible'))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(read).toHaveBeenCalledTimes(4)
  })
})

describe('requestRefresh', () => {
  it('marks stale at once, reads the first push now, and collapses a burst into one trailing read', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
    const read = vi.fn(async () => 1)
    qc.setQueryData(['p'], 0)
    const unsub = new QueryObserver(qc, { queryKey: ['p'], queryFn: read }).subscribe(() => {})
    const push = () => requestRefresh(qc, 'p', ['p'], 2_000)
    push(); push(); push()
    await vi.advanceTimersByTimeAsync(0)
    expect(read).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2_000)
    expect(read).toHaveBeenCalledTimes(2)
    unsub()
  })

  it('only marks stale while hidden', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
    const read = vi.fn(async () => 1)
    qc.setQueryData(['h'], 0)
    const unsub = new QueryObserver(qc, { queryKey: ['h'], queryFn: read }).subscribe(() => {})
    setVisibility('hidden')
    requestRefresh(qc, 'h', ['h'], 2_000)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(read).not.toHaveBeenCalled()
    expect(qc.getQueryState(['h'])?.isInvalidated).toBe(true)
    unsub()
  })

  /** A server whose answer is whatever `server.value` was when the request STARTED. */
  const setup = () => {
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
    const server = { value: 'v0' }
    const pending: Array<() => void> = []
    const read = vi.fn(() => {
      const answer = server.value
      return new Promise<string>(resolve => { pending.push(() => resolve(answer)) })
    })
    qc.setQueryData(['q'], 'v0')
    const unsub = new QueryObserver(qc, { queryKey: ['q'], queryFn: read }).subscribe(() => {})
    const settle = async () => { pending.splice(0).forEach(f => f()); await vi.advanceTimersByTimeAsync(0) }
    return { qc, server, read, unsub, settle, push: () => requestRefresh(qc, 'q', ['q'], 2_000) }
  }

  it('a push during an in-flight read owes one follow-up read with the new data', async () => {
    const { qc, server, read, unsub, settle, push } = setup()
    server.value = 'v1'
    push()
    expect(read).toHaveBeenCalledTimes(1)
    server.value = 'v2'
    push()
    await vi.advanceTimersByTimeAsync(2_000)
    await settle()
    await vi.advanceTimersByTimeAsync(5_000)
    await settle()
    expect(read).toHaveBeenCalledTimes(2)
    expect(qc.getQueryData(['q'])).toBe('v2')
    expect(qc.getQueryState(['q'])?.isInvalidated).toBe(false)
    unsub()
  })

  it('several pushes during one read owe exactly one follow-up', async () => {
    const { read, unsub, settle, push } = setup()
    push()
    for (let i = 0; i < 4; i++) { push(); await vi.advanceTimersByTimeAsync(1_000) }
    await settle()
    await vi.advanceTimersByTimeAsync(5_000)
    await settle()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(read).toHaveBeenCalledTimes(2)
    unsub()
  })

  it('a timer set while visible does not read after the document is hidden', async () => {
    const { qc, read, unsub, settle, push } = setup()
    push()
    await settle()
    push()
    setVisibility('hidden')
    await vi.advanceTimersByTimeAsync(5_000)
    await settle()
    expect(read).toHaveBeenCalledTimes(1)
    expect(qc.getQueryState(['q'])?.isInvalidated).toBe(true)
    unsub()
  })
})


/** A server whose answer is what `server.value` was when the request STARTED. */
const makeServer = () => {
  const server = { value: 'v0' }
  const pending: Array<() => void> = []
  const read = vi.fn(() => {
    const answer = server.value
    return new Promise<string>(resolve => { pending.push(() => resolve(answer)) })
  })
  const settle = async () => { for (let i = 0; i < 3; i++) { pending.splice(0).forEach(f => f()); await vi.advanceTimersByTimeAsync(3_000) } }
  const resolveNow = async () => { for (let i = 0; i < 3; i++) { pending.splice(0).forEach(f => f()); await vi.advanceTimersByTimeAsync(0) } }
  return { server, read, settle, resolveNow }
}
const cells = [true, false].flatMap(hidden => [true, false].flatMap(inFlight => [true, false].map(push => ({ hidden, inFlight, push }))))
const label = (c: { hidden: boolean; inFlight: boolean; push: boolean }) => `${c.hidden ? 'hidden' : 'visible'}, ${c.inFlight ? 'read in flight' : 'idle'}, ${c.push ? 'push' : 'no push'}`

describe('a push is never lost: {hidden, visible} x {read in flight, idle} x {push, no push}', () => {
  it.each(cells.map(c => [label(c), c] as const))('%s', async (_name, { hidden, inFlight, push }) => {
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
    const { server, read, settle } = makeServer()
    qc.setQueryData(['t'], 'v0')
    const unsub = new QueryObserver(qc, { queryKey: ['t'], queryFn: read }).subscribe(() => {})
    if (inFlight) void qc.refetchQueries({ queryKey: ['t'] })
    if (hidden) setVisibility('hidden')
    if (push) { server.value = 'v1'; requestRefresh(qc, 't', ['t'], 2_000) }
    await settle()
    if (hidden) { setVisibility('visible'); await settle() }
    await settle()
    expect(read).toHaveBeenCalledTimes((inFlight ? 1 : 0) + (push ? 1 : 0))
    expect(qc.getQueryData(['t'])).toBe(push ? 'v1' : 'v0')
    unsub()
  })
})

describe('a polled key re-reads once on return: {hidden then shown, stays visible} x {read in flight, idle}', () => {
  const polled = [true, false].flatMap(away => [true, false].map(inFlight => ({ away, inFlight })))
  it.each(polled.map(c => [`${c.away ? 'hidden then shown' : 'stays visible'}, ${c.inFlight ? 'read in flight' : 'idle'}`, c] as const))('%s', async (_name, { away, inFlight }) => {
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
    const { server, read, resolveNow } = makeServer()
    function Probe() {
      const refetchInterval = usePollRule('t2', 10_000, true, ['t2'])
      useQuery({ queryKey: ['t2'], queryFn: read, refetchInterval })
      return null
    }
    render(<QueryClientProvider client={qc}><Probe /></QueryClientProvider>)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(read).toHaveBeenCalledTimes(1)
    if (!inFlight) await act(async () => { await resolveNow() })
    if (away) act(() => setVisibility('hidden'))
    server.value = 'v1'
    if (away) { act(() => setVisibility('visible')) }
    await act(async () => { await resolveNow() })
    expect(read).toHaveBeenCalledTimes(away ? 2 : 1)
    expect(qc.getQueryData(['t2'])).toBe(away ? 'v1' : 'v0')
  })
})
