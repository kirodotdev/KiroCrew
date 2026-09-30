import { describe, it, expect, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { CHAT_FOLDERS_WRITE_KEY, invalidateFoldersWhenIdle } from '../api/chatFoldersWrite'

/**
 * The folder-tree refetch owed after optimistic folder writes.
 *
 * Writes fired together (expanding every collapsed ancestor of a folder) settle
 * together, and each still counts as pending inside its own `onSettled`. A check
 * made inline there saw the siblings pending in every callback, so none of them
 * refetched and a generation move the frame handler had deferred was lost.
 */

// A zero-delay macrotask flush: invalidateFoldersWhenIdle schedules its idle
// check with setTimeout(..., 0), so one already-queued macrotask drains it.
// Not a positive-duration sleep (frontend-tests-are-deterministic): there is
// nothing to wait OUT, only the queued check to let run.
const flush = () => new Promise(resolve => setTimeout(resolve, 0))

function writes(qc: QueryClient, n: number) {
  const releases: Array<() => void> = []
  const runs = Array.from({ length: n }, () => qc.getMutationCache().build(qc, {
    mutationKey: CHAT_FOLDERS_WRITE_KEY,
    mutationFn: () => new Promise<void>(resolve => { releases.push(resolve) }),
    onSettled: () => invalidateFoldersWhenIdle(qc),
  }).execute(undefined as never))
  return { releases, runs }
}

describe('invalidateFoldersWhenIdle', () => {
  it('refetches once when sibling writes settle in the same round', async () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { releases, runs } = writes(qc, 3)
    await Promise.resolve()
    releases.forEach(r => r())
    await Promise.all(runs)
    await flush()
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith({ queryKey: ['chat-folders'] })
  })

  it('waits for a write that is still in flight', async () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    const { releases, runs } = writes(qc, 2)
    await Promise.resolve()
    releases[0]()
    await runs[0]
    await flush()
    expect(spy).not.toHaveBeenCalled()
    releases[1]()
    await runs[1]
    await flush()
    expect(spy).toHaveBeenCalledTimes(1)
  })
})
