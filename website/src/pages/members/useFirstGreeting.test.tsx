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
  it('asks once the mate thread is confirmed', async () => {
    renderHook(() => useFirstGreeting('mate', 'member-mate', true), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledWith('mate'))
    expect(greet).toHaveBeenCalledTimes(1)
  })

  it('asks for a mate the page enabled it for, by that mate\'s slug, once', async () => {
    const { rerender } = renderHook(({ slot }) => useFirstGreeting('scout', slot, true), {
      wrapper,
      initialProps: { slot: 'member-scout' },
    })
    await waitFor(() => expect(greet).toHaveBeenCalledWith('scout'))
    rerender({ slot: 'member-scout' })
    await act(async () => {})
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

  // The first Mate's entrance (#23207) keys on this callback: only the answer
  // that means "dispatched now" fires it, so the entrance runs in exactly the
  // one tab the server's once-only claim picked, and never on a replay.
  it('onStarted fires with the slot for `started`, the one answer that dispatched a greeting', async () => {
    const onStarted = vi.fn()
    renderHook(() => useFirstGreeting('mate', 'member-mate-entrance', true, onStarted), { wrapper })
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith('member-mate-entrance'))
    expect(onStarted).toHaveBeenCalledTimes(1)
  })

  it.each(['already_greeted', 'not_empty', 'busy', 'not_owed', 'no_thread', 'not_crewmate'])(
    'onStarted stays silent on `%s`',
    async (outcome) => {
      greet.mockResolvedValueOnce({ outcome })
      const onStarted = vi.fn()
      renderHook(() => useFirstGreeting('mate', `member-mate-${outcome}`, true, onStarted), { wrapper })
      await waitFor(() => expect(greet).toHaveBeenCalledTimes(1))
      await act(async () => { await Promise.resolve() })
      expect(onStarted).not.toHaveBeenCalled()
    },
  )

  it('onStarted stays silent on a failed request', async () => {
    greet.mockRejectedValueOnce(new Error('offline'))
    const onStarted = vi.fn()
    renderHook(() => useFirstGreeting('mate', 'member-mate-failed', true, onStarted), { wrapper })
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(1))
    await act(async () => { await Promise.resolve() })
    expect(onStarted).not.toHaveBeenCalled()
  })

  it('the latest onStarted hears the answer; a new callback never asks again', async () => {
    let resolve: (v: { outcome: string }) => void = () => {}
    greet.mockImplementationOnce(() => new Promise((res) => { resolve = res }))
    const first = vi.fn()
    const second = vi.fn()
    const { rerender } = renderHook(
      ({ cb }: { cb: (slot: string) => void }) => useFirstGreeting('mate', 'member-mate-latest', true, cb),
      { wrapper, initialProps: { cb: first } },
    )
    await waitFor(() => expect(greet).toHaveBeenCalledTimes(1))
    rerender({ cb: second })
    await act(async () => { resolve({ outcome: 'started' }) })
    await waitFor(() => expect(second).toHaveBeenCalledWith('member-mate-latest'))
    expect(first).not.toHaveBeenCalled()
    expect(greet).toHaveBeenCalledTimes(1)
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
