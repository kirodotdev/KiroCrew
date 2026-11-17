/**
 * How `useAgents` spends requests on `GET /api/agents/catalog`.
 *
 * Every pane's picker and the page's own roster read the catalog on one refresh, and
 * a hidden tab has nobody to show the answer to. The reads that stay: the first one,
 * a scope switch, a `reload`, and a refresh while the tab is visible, which still goes
 * out at once so a change shows as fast as before.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook as rtlRenderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useAgents, __resetCatalogReadsForTests } from '../hooks/useAgents'

vi.mock('../api/client', () => ({
  api: {
    agentCatalog: vi.fn(),
    kirocrewConfig: vi.fn(() => Promise.resolve({})),
  },
}))

const renderHook = ((cb: never, opts?: Record<string, unknown>) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
  return rtlRenderHook(cb, { ...opts, wrapper })
}) as typeof rtlRenderHook

const { api } = await import('../api/client')
const catalog = api.agentCatalog as unknown as ReturnType<typeof vi.fn>

const reply = (names: string[]) => ({
  agents: names.map(name => ({ name, scope: 'global', selection_kind: 'template' })),
  default_agent: names[0] ?? '',
})

let hidden = false
function setHidden(value: boolean) {
  hidden = value
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  vi.clearAllMocks()
  __resetCatalogReadsForTests()
  hidden = false
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  catalog.mockResolvedValue(reply(['kirocrew']))
})

afterEach(() => {
  delete (document as unknown as Record<string, unknown>).hidden
})

describe('useAgents refresh reads', () => {
  it('reads again at once when the counter moves on a visible tab', async () => {
    const { rerender } = renderHook(({ n }: { n: number }) => useAgents(n, 'chat-1'), { initialProps: { n: 0 } })
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(1))
    rerender({ n: 1 })
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(2))
  })

  it('two readers of one chat share the read of one refresh', async () => {
    const { rerender } = renderHook(({ n }: { n: number }) => {
      useAgents(n, 'chat-1')
      useAgents(n, 'chat-1')
    }, { initialProps: { n: 0 } })
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(1))
    // Both readers' effects ran inside `rerender`'s act, so a second request would
    // already have gone out by now.
    rerender({ n: 1 })
    expect(catalog).toHaveBeenCalledTimes(2)
  })

  it('readers of different chats each read, because the answer names the chat', async () => {
    renderHook(() => {
      useAgents(0, 'chat-1')
      useAgents(0, 'chat-2')
    })
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(2))
    expect(catalog.mock.calls.map(c => c[0]).sort()).toEqual(['chat-1', 'chat-2'])
  })

  it('does not read for a refresh while the tab is hidden, and reads once when it is shown', async () => {
    const { rerender } = renderHook(({ n }: { n: number }) => useAgents(n, 'chat-1'), { initialProps: { n: 0 } })
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(1))
    act(() => setHidden(true))
    rerender({ n: 1 })
    rerender({ n: 2 })
    expect(catalog).toHaveBeenCalledTimes(1)
    act(() => setHidden(false))
    expect(catalog).toHaveBeenCalledTimes(2)
    // One read for the whole hidden stretch, and the listener is gone.
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(catalog).toHaveBeenCalledTimes(2)
  })

  it('shows what the catalog said while hidden once the tab is shown', async () => {
    catalog.mockResolvedValueOnce(reply(['kirocrew']))
    const { result, rerender } = renderHook(({ n }: { n: number }) => useAgents(n, 'chat-1'), { initialProps: { n: 0 } })
    await waitFor(() => expect(result.current.agents.map(a => a.name)).toEqual(['kirocrew']))
    act(() => setHidden(true))
    catalog.mockResolvedValue(reply(['kirocrew', 'added']))
    rerender({ n: 1 })
    expect(result.current.agents.map(a => a.name)).toEqual(['kirocrew'])
    act(() => setHidden(false))
    await waitFor(() => expect(result.current.agents.map(a => a.name)).toEqual(['kirocrew', 'added']))
  })

  it('still reads at once for a scope switch and for a reload while hidden', async () => {
    const { result, rerender } = renderHook(({ key }: { key: string }) => useAgents(0, key), { initialProps: { key: 'chat-1' } })
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(1))
    act(() => setHidden(true))
    rerender({ key: 'chat-2' })
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(2))
    act(() => result.current.reload())
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(3))
  })

  it('reads for the first time even when the tab starts hidden', async () => {
    hidden = true
    renderHook(() => useAgents(0, 'chat-1'))
    await waitFor(() => expect(catalog).toHaveBeenCalledTimes(1))
  })
})
