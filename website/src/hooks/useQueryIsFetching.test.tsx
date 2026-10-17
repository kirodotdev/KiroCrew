import { act, render, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { useQueryIsFetching } from './useQueryIsFetching'

function setup(key: readonly unknown[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  let renders = 0
  const hook = renderHook(({ k }) => { renders += 1; return useQueryIsFetching(k) }, {
    wrapper, initialProps: { k: key },
  })
  return { client, hook, renders: () => renders }
}

function deferred() {
  let resolve!: (v: string) => void
  const promise = new Promise<string>(r => { resolve = r })
  return { promise, resolve }
}

describe('useQueryIsFetching', () => {
  it('is false for a key that is not cached', () => {
    const { hook } = setup(['session-automation', 'a'])
    expect(hook.result.current).toBe(false)
  })

  it('follows the exact key through a fetch', async () => {
    const { client, hook } = setup(['session-automation', 'a'])
    const d = deferred()
    let done!: Promise<unknown>
    act(() => { done = client.fetchQuery({ queryKey: ['session-automation', 'a'], queryFn: () => d.promise }) })
    await waitFor(() => expect(hook.result.current).toBe(true))
    await act(async () => { d.resolve('x'); await done })
    await waitFor(() => expect(hook.result.current).toBe(false))
  })

  it('ignores other keys, including a longer key with the same prefix', async () => {
    const { client, hook, renders } = setup(['session-automation', 'a'])
    const before = renders()
    const d = deferred()
    act(() => {
      void client.fetchQuery({ queryKey: ['session-automation', 'a', 'extra'], queryFn: () => d.promise })
      void client.fetchQuery({ queryKey: ['session-automation', 'b'], queryFn: () => d.promise })
    })
    expect(hook.result.current).toBe(false)
    expect(renders()).toBe(before)
    await act(async () => { d.resolve('x') })
  })

  it('re-targets when the key changes', async () => {
    const { client, hook } = setup(['session-automation', 'a'])
    const d = deferred()
    act(() => { void client.fetchQuery({ queryKey: ['session-automation', 'b'], queryFn: () => d.promise }) })
    expect(hook.result.current).toBe(false)
    hook.rerender({ k: ['session-automation', 'b'] })
    expect(hook.result.current).toBe(true)
    await act(async () => { d.resolve('x') })
    await waitFor(() => expect(hook.result.current).toBe(false))
  })

  it('does not update during another component\'s render when that render changes the query', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const key = ['session-automation', 'a']
    const d = deferred()
    function Watcher() { return <span>{String(useQueryIsFetching(key))}</span> }
    // A cache change made while Reader renders, standing in for any
    // render-phase cache event, such as the 'added' a useQuery emits when it
    // builds an evicted key.
    function Reader() { void client.fetchQuery({ queryKey: key, queryFn: () => d.promise }); return null }
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const view = render(
      <QueryClientProvider client={client}><Watcher /></QueryClientProvider>,
    )
    view.rerender(
      <QueryClientProvider client={client}><Watcher /><Reader /></QueryClientProvider>,
    )
    await waitFor(() => expect(view.container.textContent).toBe('true'))
    await act(async () => { d.resolve('x') })
    const renderPhaseUpdates = errors.mock.calls.filter(args =>
      String(args[0]).includes('Cannot update a component'))
    errors.mockRestore()
    expect(renderPhaseUpdates).toEqual([])
  })
})
