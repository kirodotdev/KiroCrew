import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'

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
    expect(hook.result.current).toBe(true)
    await act(async () => { d.resolve('x'); await done })
    expect(hook.result.current).toBe(false)
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
    expect(hook.result.current).toBe(false)
  })
})
