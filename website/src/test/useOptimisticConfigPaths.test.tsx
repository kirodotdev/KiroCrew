// useOptimisticConfigPaths — the shared per-path optimistic overlay (#6890),
// extracted from the Settings ▸ Chat model pickers' merged fix shape.
//
// The contract pinned here, once, for every settings panel that uses it:
//   * a save's value shows IMMEDIATELY via the overlay, while the query cache
//     is untouched (no whole-object onMutate snapshot exists to restore);
//   * a failure stops masking only its OWN path — a concurrent save on a
//     sibling path sharing the query key keeps its in-flight display;
//   * ownership is a monotonic token: a superseded save's slow success
//     neither clears the newer pending entry nor writes its stale value into
//     the cache, and its late failure reports nothing;
//   * the error path still refetches (a request can fail after persisting);
//   * a fresh attempt on a path fires onSupersede with that path, so the
//     panel can clear that path's stale failure state.
//
// Panel-level race tests (DisplayPanel tint/shell, SkillsPanel, ChatPanel
// tips/dashboard) assert the conversion of each call site; the mechanism
// itself is pinned here against a minimal two-path harness.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import React, { useState } from 'react'

import { useOptimisticConfigPaths, setConfigPathValue } from '../pages/settings/useOptimisticConfigPaths'

type Cfg = { a?: string; b?: string }

const fetchCfg = vi.fn<() => Promise<Cfg>>()
const patchA = vi.fn<(v: string) => Promise<unknown>>()
const patchB = vi.fn<(v: string) => Promise<unknown>>()
const onFailureA = vi.fn()
const onSupersedeA = vi.fn()

/** Two mutations on sibling paths of ONE query key — the defect's shape. */
function Harness() {
  const qc = useQueryClient()
  const cfgQ = useQuery<Cfg>({ queryKey: ['cfg'], queryFn: fetchCfg })
  const overlay = useOptimisticConfigPaths(qc)
  const [errA, setErrA] = useState('')
  const mutA = useMutation(overlay.mutationOpts<string>({
    queryKey: ['cfg'],
    mutationFn: patchA,
    path: () => 'a',
    displayValue: v => v,
    applyToCache: (cached, v) => setConfigPathValue(cached as Cfg, 'a', v),
    onFailure: (err, v) => { onFailureA(err, v); setErrA('save of a failed') },
    onSupersede: p => { onSupersedeA(p); setErrA('') },
  }))
  const mutB = useMutation(overlay.mutationOpts<string>({
    queryKey: ['cfg'],
    mutationFn: patchB,
    path: () => 'b',
    displayValue: v => v,
    applyToCache: (cached, v) => setConfigPathValue(cached as Cfg, 'b', v),
  }))
  return (
    <div>
      <output data-testid="a">{overlay.shown('a', cfgQ.data?.a ?? '')}</output>
      <output data-testid="b">{overlay.shown('b', cfgQ.data?.b ?? '')}</output>
      <output data-testid="errA">{errA}</output>
      <button onClick={() => mutA.mutate('a1')}>saveA1</button>
      <button onClick={() => mutA.mutate('a2')}>saveA2</button>
      <button onClick={() => mutB.mutate('b1')}>saveB1</button>
    </div>
  )
}

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><Harness /></QueryClientProvider>)
}

/** A request whose settle the test controls — the in-flight window. */
function defer(mock: ReturnType<typeof vi.fn>) {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  mock.mockImplementationOnce(() => promise as never)
  return { resolve, reject }
}

beforeEach(() => {
  fetchCfg.mockReset().mockImplementation(() => Promise.resolve({ a: 'a0', b: 'b0' }))
  patchA.mockReset().mockImplementation(() => Promise.resolve({}))
  patchB.mockReset().mockImplementation(() => Promise.resolve({}))
  onFailureA.mockClear()
  onSupersedeA.mockClear()
})

describe('useOptimisticConfigPaths — contract', () => {
  it('shows the pending value immediately, without touching the query cache', async () => {
    const dA = defer(patchA)
    wrap()
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a0'))
    fireEvent.click(screen.getByText('saveA1'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a1'))
    // Pre-settle: only the initial fetch has run — the display can only be
    // coming from the overlay, never from an onMutate cache write.
    expect(fetchCfg).toHaveBeenCalledTimes(1)
    expect(onSupersedeA).toHaveBeenCalledWith('a')
    dA.resolve({})
    await waitFor(() => expect(fetchCfg).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('a')).toHaveTextContent('a0')
  })

  it("a failure rolls back only its own path — a sibling save's in-flight display survives", async () => {
    const dB = defer(patchB)
    const dA = defer(patchA)
    wrap()
    await waitFor(() => expect(screen.getByTestId('b')).toHaveTextContent('b0'))
    fireEvent.click(screen.getByText('saveB1'))
    fireEvent.click(screen.getByText('saveA1'))
    await waitFor(() => expect(screen.getByTestId('b')).toHaveTextContent('b1'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a1'))

    // A fails while B is still in flight: A rolls back, B must not move.
    dA.reject(new Error('boom'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a0'))
    expect(screen.getByTestId('b')).toHaveTextContent('b1')
    expect(screen.getByTestId('errA')).toHaveTextContent('save of a failed')
    expect(onFailureA).toHaveBeenCalledTimes(1)
    dB.resolve({})
  })

  it("a superseded save's slow success neither clears the newer pending nor writes the cache", async () => {
    const d1 = defer(patchA)
    const d2 = defer(patchA)
    wrap()
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a0'))
    fireEvent.click(screen.getByText('saveA1'))
    fireEvent.click(screen.getByText('saveA2'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a2'))

    // The OLDER save settles first. Its success must not clear the newer
    // pending entry (display keeps a2) and must not write a1 into the cache:
    // the refetch it triggers still reports the original server value, so a
    // wrongful write would be visible once the newer entry clears.
    d1.resolve({})
    await waitFor(() => expect(fetchCfg).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('a')).toHaveTextContent('a2')

    // The newer save fails; only now does the display fall back — to the
    // server's value, not to the superseded save's a1.
    d2.reject(new Error('boom'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a0'))
  })

  it("a superseded save's late failure reports nothing — the newer save owns the path", async () => {
    const d1 = defer(patchA)
    const d2 = defer(patchA)
    wrap()
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a0'))
    fireEvent.click(screen.getByText('saveA1'))
    fireEvent.click(screen.getByText('saveA2'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a2'))

    d1.reject(new Error('boom'))
    fetchCfg.mockImplementation(() => Promise.resolve({ a: 'a2', b: 'b0' }))
    d2.resolve({})
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a2'))
    expect(onFailureA).not.toHaveBeenCalled()
    expect(screen.getByTestId('errA')).toHaveTextContent('')
  })

  it('refetches after a failure, so a server-side apply is not rolled back blind', async () => {
    const dA = defer(patchA)
    wrap()
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a0'))
    fireEvent.click(screen.getByText('saveA1'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a1'))

    // The server persisted the write before answering 5xx: the error-path
    // refetch is what brings the surviving value back.
    fetchCfg.mockImplementation(() => Promise.resolve({ a: 'a1', b: 'b0' }))
    dA.reject(new Error('boom'))
    await waitFor(() => expect(fetchCfg).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a1'))
  })

  it("an unmounted instance's late success never writes the cache over a newer instance's save", async () => {
    // The ownership tokens are per hook INSTANCE, but the query cache is
    // global: a save begun before an unmount → remount still passes its own
    // instance's token check when it settles late, because that dead ref
    // never saw the new instance's saves. The mounted guard is what keeps it
    // from writing a stale value the new instance's save already superseded
    // on the server.
    const d1 = defer(patchA)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const first = render(<QueryClientProvider client={qc}><Harness /></QueryClientProvider>)
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a0'))
    fireEvent.click(screen.getByText('saveA1'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a1'))

    // Tab switch: the panel unmounts with the save still in flight, then a
    // fresh instance mounts against the SAME query client.
    first.unmount()
    const d2 = defer(patchA)
    render(<QueryClientProvider client={qc}><Harness /></QueryClientProvider>)
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a0'))

    // The new instance saves a2; the server accepts and reports it.
    fireEvent.click(screen.getByText('saveA2'))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a2'))
    fetchCfg.mockImplementation(() => Promise.resolve({ a: 'a2', b: 'b0' }))
    d2.resolve({})
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a2'))

    // The DEAD instance's save settles last, and its settle-time refetch
    // fails — the one condition under which a wrongful cache write would
    // stick and show. The display must stay on the newer accepted value.
    fetchCfg.mockImplementationOnce(() => Promise.reject(new Error('offline')))
    d1.resolve({})
    await waitFor(() => expect(fetchCfg.mock.calls.length).toBeGreaterThan(2))
    await waitFor(() => expect(screen.getByTestId('a')).toHaveTextContent('a2'))
  })
})
