import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// The hook's placeholder runs BEFORE the first fetch resolves, so the fetcher is
// stubbed to a promise that never settles: that pins the render to exactly the
// pre-resolution state the placeholder exists for.
const fetchAvailableModels = vi.fn()

vi.mock('../providers', () => ({
  useProvider: () => ({ id: 'acp', fetchAvailableModels }),
}))

import { useAvailableModels } from '../hooks/useAvailableModels'

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('useAvailableModels placeholder before the first fetch resolves', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('offers auto when nothing is known yet, which is the kiro answer', () => {
    // agent.acp_backend defaults to kiro and kiro is the floor: a browser that
    // has never seen a response must behave as it did before any adapter
    // existed (harness-parity H1/H4).
    fetchAvailableModels.mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useAvailableModels(), { wrapper })
    expect(result.current.map((m) => m.name)).toEqual(['auto'])
  })

  it('withholds auto once the gateway has reported a backend without it', () => {
    // The synthetic row is the picker's FIRST paint. Left unconditional it
    // undoes everything the fetcher does to avoid fabricating `auto`, because
    // this branch runs before any fetch lands: the operator sees one row, picks
    // the only thing on offer, and the turn fails at the wire with -32603.
    localStorage.setItem('kc.acp.servesAuto.v1', '0')
    fetchAvailableModels.mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useAvailableModels(), { wrapper })
    expect(result.current).toEqual([])
  })

  it('keeps auto for a non-kiro backend that serves it', () => {
    localStorage.setItem('kc.acp.servesAuto.v1', '1')
    fetchAvailableModels.mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useAvailableModels(), { wrapper })
    expect(result.current.map((m) => m.name)).toEqual(['auto'])
  })

  it('returns a STABLE empty array across renders', async () => {
    // A fresh `[]` per call is a new identity every render, which turns any
    // effect or memo keyed on the list into a re-render loop.
    localStorage.setItem('kc.acp.servesAuto.v1', '0')
    fetchAvailableModels.mockReturnValue(new Promise(() => {}))
    const { result, rerender } = renderHook(() => useAvailableModels(), { wrapper })
    const first = result.current
    rerender()
    expect(result.current).toBe(first)
  })

  it('yields to the real list as soon as it resolves', async () => {
    localStorage.setItem('kc.acp.servesAuto.v1', '0')
    fetchAvailableModels.mockResolvedValue([{ name: 'gpt-5-codex', description: 'Codex' }])
    const { result } = renderHook(() => useAvailableModels(), { wrapper })
    await waitFor(() => expect(result.current.map((m) => m.name)).toEqual(['gpt-5-codex']))
  })

  it('does not flash auto after the default harness moves to an adapter', () => {
    // lastKnown is still kiro (or absent). Showing Auto here is the first-paint
    // bug: the operator picks the only row and the adapter rejects it.
    fetchAvailableModels.mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useAvailableModels({ backend: 'codex' }), { wrapper })
    expect(result.current).toEqual([])
  })

  it('scopes the fetch to a live slot so an open chat keeps its own catalog', async () => {
    fetchAvailableModels.mockResolvedValue([{ name: 'gpt-5.6-sol', description: 'Kiro' }])
    const { result } = renderHook(
      () => useAvailableModels({ slot: 'chat-1', backend: '' }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.map((m) => m.name)).toEqual(['gpt-5.6-sol']))
    expect(fetchAvailableModels).toHaveBeenCalledWith({ slot: 'chat-1', scope: 'slot:chat-1' })
  })

  it('withholds auto for the sidebar bulk switcher on an adapter live session', () => {
    // ChatSidebar now passes the live slot + harness. The first paint must not
    // offer Auto when that harness does not serve it.
    fetchAvailableModels.mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(
      () => useAvailableModels({ enabled: true, slot: 'chat-9', backend: 'opencode' }),
      { wrapper },
    )
    expect(result.current).toEqual([])
    expect(fetchAvailableModels).toHaveBeenCalledWith({ slot: 'chat-9', scope: 'slot:chat-9' })
  })
})
