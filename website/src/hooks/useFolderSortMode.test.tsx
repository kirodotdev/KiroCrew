/**
 * `useFolderSortRead`: the ONE derivation of what a surface knows about the
 * folder sort mode, exercised against a real react-query client so the query
 * states it keys on are the library's own, not a hand-written imitation.
 *
 * The contract, in the order the states occur in production:
 *  - a body on hand is KNOWN and silent -- fresh, cached, or kept across a failed
 *    background refetch (react-query retains the previous body and retries on
 *    its own, so there is nothing to tell the person);
 *  - a failure with NO body is said, with the server's own words;
 *  - that failure is LATCHED: the retry's pending phase (no body, no error)
 *    keeps it, so a banner keyed on it does not unmount and remount around
 *    every automatic retry; a body arriving clears it.
 */
import { describe, it, expect } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useFolderSortRead, type FolderSortConfigBody } from './useFolderSortMode'

const KEY = ['kirocrewConfig']

function harness(queryFn: () => Promise<FolderSortConfigBody>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const hook = renderHook(
    () => useFolderSortRead(useQuery<FolderSortConfigBody>({ queryKey: KEY, queryFn })),
    { wrapper },
  )
  return { qc, hook }
}

describe('useFolderSortRead', () => {
  it('knows the mode from a body on hand, and reads an absent or unknown value as custom', async () => {
    const { hook } = harness(async () => ({ dashboard: { folder_sort: 'name' } }))
    await waitFor(() => expect(hook.result.current.known).toBe(true))
    expect(hook.result.current.mode).toBe('name')
    expect(hook.result.current.error).toBeNull()
    const bare = harness(async () => ({}))
    await waitFor(() => expect(bare.hook.result.current.known).toBe(true))
    expect(bare.hook.result.current.mode).toBe('custom')
  })

  it('is not known while the first read is in flight, and says nothing yet', () => {
    const { hook } = harness(() => new Promise(() => {}))
    expect(hook.result.current.known).toBe(false)
    expect(hook.result.current.error).toBeNull()
    expect(hook.result.current.mode).toBe('custom')
  })

  it('stays known and silent across a failed background refetch: the cached body is what it knows', async () => {
    let fail = false
    const { qc, hook } = harness(async () => {
      if (fail) throw new Error('config store unavailable')
      return { dashboard: { folder_sort: 'custom' } }
    })
    await waitFor(() => expect(hook.result.current.known).toBe(true))
    fail = true
    await act(async () => { await qc.refetchQueries({ queryKey: KEY }) })
    expect(qc.getQueryState(KEY)?.status).toBe('error')
    expect(hook.result.current.known).toBe(true)
    expect(hook.result.current.mode).toBe('custom')
    expect(hook.result.current.error).toBeNull()
  })

  it('says a failure with no body, latches it through the retry, and clears it when a body arrives', async () => {
    let settle: (v: FolderSortConfigBody) => void = () => {}
    let attempt = 0
    const { qc, hook } = harness(() => {
      attempt += 1
      if (attempt === 1) return Promise.reject(new Error('gateway restarting'))
      return new Promise<FolderSortConfigBody>(resolve => { settle = resolve })
    })
    await waitFor(() => expect(hook.result.current.error).toBe('gateway restarting'))
    expect(hook.result.current.known).toBe(false)
    // The retry: pending, no body, no error -- the latch carries the text.
    act(() => { void qc.refetchQueries({ queryKey: KEY }) })
    await waitFor(() => expect(qc.getQueryState(KEY)?.fetchStatus).toBe('fetching'))
    expect(qc.getQueryState(KEY)?.status).toBe('pending')
    expect(hook.result.current.error).toBe('gateway restarting')
    expect(hook.result.current.known).toBe(false)
    // A body arrives: known, silent, the person's mode.
    await act(async () => { settle({ dashboard: { folder_sort: 'name' } }) })
    await waitFor(() => expect(hook.result.current.error).toBeNull())
    expect(hook.result.current.known).toBe(true)
    expect(hook.result.current.mode).toBe('name')
  })

  it('falls back to the generic message when the failure carries no words', async () => {
    const { hook } = harness(() => Promise.reject(new Error('')))
    await waitFor(() => expect(hook.result.current.error).not.toBeNull())
    expect(hook.result.current.error).toBe('Something went wrong')
  })

  it('a FRESH observer mounted mid-retry still reports the failure on record', async () => {
    // A consumer that opens after the read failed (the job form, the header
    // menu) mounts a new hook instance and, by default, refetches on mount --
    // which puts a data-less query back into `pending` with its error cleared.
    // The instance has no latch of its own yet, so the failure on record has to
    // come from the query: `errorUpdatedAt` survives the re-pending. Until the
    // retry settles the words are the generic ones; then the server's return.
    let settle: (v: FolderSortConfigBody) => void = () => {}
    let attempt = 0
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const queryFn = () => {
      attempt += 1
      if (attempt === 1) return Promise.reject(new Error('gateway restarting'))
      return new Promise<FolderSortConfigBody>(resolve => { settle = resolve })
    }
    const first = renderHook(
      () => useFolderSortRead(useQuery<FolderSortConfigBody>({ queryKey: KEY, queryFn })),
      { wrapper },
    )
    await waitFor(() => expect(first.result.current.error).toBe('gateway restarting'))
    // The second observer: its mount is what starts the retry.
    const second = renderHook(
      () => useFolderSortRead(useQuery<FolderSortConfigBody>({ queryKey: KEY, queryFn })),
      { wrapper },
    )
    await waitFor(() => expect(qc.getQueryState(KEY)?.fetchStatus).toBe('fetching'))
    expect(qc.getQueryState(KEY)?.status).toBe('pending')
    expect(second.result.current.known).toBe(false)
    expect(second.result.current.error).toBe('Something went wrong')
    // The first instance keeps the words it latched.
    expect(first.result.current.error).toBe('gateway restarting')
    // The retry fails again with new words: both instances carry them.
    // (Rejecting the retry's promise is not possible through `settle`; a body
    // instead, and both instances clear.)
    await act(async () => { settle({ dashboard: { folder_sort: 'name' } }) })
    await waitFor(() => expect(second.result.current.error).toBeNull())
    expect(first.result.current.error).toBeNull()
    expect(second.result.current.mode).toBe('name')
  })
})
