/**
 * A window blur and focus with a positive barrier after it.
 *
 * react-query answers a focus by walking every query in the cache in one pass and
 * refetching each stale one an enabled observer holds. A test that asserts a
 * query was NOT refetched on a focus needs proof that the pass ran, not a sleep:
 * this registers a sentinel query that is always stale and focus-enabled, and
 * returns once the focus has refetched it. Every other query was visited in the
 * same pass, so an absence assertion made after this reads a settled state.
 *
 * Exactly one pass: the focus is the return from a forced blur to the page's own
 * state (`setFocused(undefined)`), which emits once and leaves nothing forced. A
 * blur, a forced focus and then a reset would emit twice, the second pass still
 * in flight when the barrier returns.
 *
 * Under fake timers pass `{ fakeTimers: true }`: `waitFor` cannot advance a fake
 * clock, so the barrier flushes the pending promises through it instead.
 */
import { vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { focusManager, QueryObserver, type QueryClient } from '@tanstack/react-query'

export async function refocusWindow(qc: QueryClient, opts: { fakeTimers?: boolean } = {}): Promise<void> {
  let reads = 0
  const sentinel = new QueryObserver(qc, {
    queryKey: ['__focus-barrier__'],
    queryFn: async () => ++reads,
    staleTime: 0,
    refetchOnWindowFocus: 'always',
  })
  const unsubscribe = sentinel.subscribe(() => {})
  const reached = async (n: number) => {
    if (!opts.fakeTimers) return waitFor(() => expect(reads).toBe(n))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(reads).toBe(n)
  }
  try {
    await reached(1)
    await act(async () => {
      focusManager.setFocused(false)
      focusManager.setFocused(undefined)
    })
    await reached(2)
  } finally {
    // Already `undefined` on the normal path, so this emits nothing there; it
    // only matters when a step above threw with the blur still forced.
    focusManager.setFocused(undefined)
    unsubscribe()
    qc.removeQueries({ queryKey: ['__focus-barrier__'], exact: true })
  }
}
