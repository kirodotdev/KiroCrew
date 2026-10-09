import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../api/client', () => ({
  api: { memberGreet: vi.fn(() => Promise.resolve({ outcome: 'started' })) },
}))

import { api } from '../../api/client'
import { resetFirstGreetingRequests, useFirstGreeting } from './useFirstGreeting'
import { store } from '../../store'
import { selectComposerBusy, selectSlotRunEpoch, sseChatMessage, startServerTurn } from '../../store/chatSlice'

/* The page-side trigger of a crewmate's first greeting. The server owns the
 * once-only guarantee; this hook only has to ask at the right moment (enabled,
 * confirmed thread) and not ask again for the same thread in this tab. */

const greet = api.memberGreet as ReturnType<typeof vi.fn>

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.clearAllMocks()
  resetFirstGreetingRequests()
})

describe('useFirstGreeting', () => {
  it('asks once the crewmate thread is confirmed', async () => {
    renderHook(() => useFirstGreeting('mate', 'member-mate', true), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledWith('mate'))
    expect(greet).toHaveBeenCalledTimes(1)
  })

  it('never asks while disabled', async () => {
    renderHook(() => useFirstGreeting('alpha', 'member-alpha', false), { wrapper })
    await new Promise((r) => setTimeout(r, 0))
    expect(greet).not.toHaveBeenCalled()
  })

  it('waits for the confirmed slot key before asking', async () => {
    const { rerender } = renderHook(
      ({ slot }: { slot: string }) => useFirstGreeting('mate', slot, true),
      { wrapper, initialProps: { slot: '' } },
    )
    await new Promise((r) => setTimeout(r, 0))
    expect(greet).not.toHaveBeenCalled()
    rerender({ slot: 'member-mate' })
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(1))
  })

  it('does not ask again for the same thread after a remount in this tab', async () => {
    const first = renderHook(() => useFirstGreeting('mate', 'member-mate', true), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(1))
    first.unmount()
    renderHook(() => useFirstGreeting('mate', 'member-mate', true), { wrapper })
    await new Promise((r) => setTimeout(r, 0))
    expect(greet).toHaveBeenCalledTimes(1)
  })

  it('a failed request does not ask again on its own but the next open does', async () => {
    greet.mockRejectedValueOnce(new Error('offline'))
    const first = renderHook(() => useFirstGreeting('mate', 'member-mate', true), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(1))
    await act(async () => { await Promise.resolve() })
    expect(greet).toHaveBeenCalledTimes(1)
    first.unmount()
    renderHook(() => useFirstGreeting('mate', 'member-mate', true), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(2))
  })

  it('a request that fails after the page unmounted does not block the next open', async () => {
    let reject: (e: Error) => void = () => {}
    greet.mockImplementationOnce(() => new Promise((_res, rej) => { reject = rej }))
    const first = renderHook(() => useFirstGreeting('mate', 'member-mate', true), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(1))
    first.unmount()
    await act(async () => { reject(new Error('offline')) })
    renderHook(() => useFirstGreeting('mate', 'member-mate', true), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(2))
  })

  // The greeting turn writes nothing until the model speaks, so without this a
  // message sent meanwhile drew an optimistic bubble AND the server's queued
  // copy: the user's words twice, either side of the greeting.
  it('a started greeting makes the thread composer busy before any frame', async () => {
    const slot = 'member-mate-busy'
    expect(selectComposerBusy(store.getState(), slot)).toBe(false)
    renderHook(() => useFirstGreeting('mate', slot, true), { wrapper })
    await waitFor(() => expect(selectComposerBusy(store.getState(), slot)).toBe(true))
  })

  it('a declined greeting leaves the thread idle', async () => {
    greet.mockResolvedValueOnce({ outcome: 'not_empty' })
    const slot = 'member-mate-declined'
    renderHook(() => useFirstGreeting('mate', slot, true), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(1))
    await new Promise((r) => setTimeout(r, 0))
    expect(selectComposerBusy(store.getState(), slot)).toBe(false)
  })
})

describe('startServerTurn', () => {
  it('ignores an answer older than a frame that already moved the run', () => {
    const slot = 'member-mate-stale'
    const epoch = selectSlotRunEpoch(store.getState(), slot)
    // The turn's first chunk and its `_done` both landed before the response.
    store.dispatch(sseChatMessage({ slot, role: 'chunk', content: 'Hi' } as never))
    store.dispatch(sseChatMessage({ slot, role: '_done', content: '' } as never))
    store.dispatch(startServerTurn({ slot, epoch }))
    expect(selectComposerBusy(store.getState(), slot)).toBe(false)
  })
})
