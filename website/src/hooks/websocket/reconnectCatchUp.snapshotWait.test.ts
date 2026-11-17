/** The reconnect catch-up window ends on the new socket's connect snapshot; the
 *  slot list is read over HTTP only for an empty snapshot or none at all. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { AppDispatch } from '../../store'
import { createReconnectSnapshotWait, RECONNECT_SNAPSHOT_WAIT_MS } from './reconnectCatchUp'

vi.mock('../../store/dashboardSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../store/dashboardSlice')>()
  // The thunk's action types stay, for the reducers built from them.
  return { ...actual, fetchSlots: Object.assign(() => 'fetchSlots', actual.fetchSlots) }
})

/** A dispatch whose every `fetchSlots` read is held until the test answers it. */
function harness() {
  const reads: Array<{ answer: () => void; settled: Promise<void> }> = []
  const dispatch = vi.fn((action: unknown) => {
    expect(action).toBe('fetchSlots')
    let answer!: () => void
    const settled = new Promise<void>(resolve => { answer = resolve })
    reads.push({ answer, settled })
    return settled
  }) as unknown as AppDispatch
  const reconnectingRef = { current: false }
  const wait = createReconnectSnapshotWait(dispatch, reconnectingRef)
  return { dispatch: dispatch as unknown as ReturnType<typeof vi.fn>, reads, reconnectingRef, wait }
}

describe('createReconnectSnapshotWait', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('a snapshot with rows ends the window and reads nothing', () => {
    const { dispatch, reconnectingRef, wait } = harness()
    wait.arm()
    expect(reconnectingRef.current).toBe(true)
    wait.onSlots([{ key: 'a' }])
    expect(reconnectingRef.current).toBe(false)
    vi.advanceTimersByTime(RECONNECT_SNAPSHOT_WAIT_MS)
    expect(dispatch).not.toHaveBeenCalled()
  })

  it('only the first slots frame after the open settles the window', () => {
    const { dispatch, reconnectingRef, wait } = harness()
    wait.arm()
    wait.onSlots([{ key: 'a' }])
    wait.onSlots([])
    expect(dispatch).not.toHaveBeenCalled()
    expect(reconnectingRef.current).toBe(false)
  })

  it('an empty snapshot is read once, and the window ends when the read settles', async () => {
    const { dispatch, reads, reconnectingRef, wait } = harness()
    wait.arm()
    wait.onSlots([])
    expect(dispatch).toHaveBeenCalledTimes(1)
    expect(reconnectingRef.current).toBe(true)
    reads[0].answer()
    await reads[0].settled
    expect(reconnectingRef.current).toBe(false)
    vi.advanceTimersByTime(RECONNECT_SNAPSHOT_WAIT_MS)
    expect(dispatch).toHaveBeenCalledTimes(1)
  })

  it('no snapshot within the wait reads the list, and the window ends with that read', async () => {
    const { dispatch, reads, reconnectingRef, wait } = harness()
    wait.arm()
    vi.advanceTimersByTime(RECONNECT_SNAPSHOT_WAIT_MS - 1)
    expect(dispatch).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(dispatch).toHaveBeenCalledTimes(1)
    // A snapshot after the read started changes nothing.
    wait.onSlots([{ key: 'a' }])
    expect(reconnectingRef.current).toBe(true)
    reads[0].answer()
    await reads[0].settled
    expect(reconnectingRef.current).toBe(false)
  })

  it('a socket that closes before its snapshot ends the window and reads nothing', () => {
    const { dispatch, reconnectingRef, wait } = harness()
    wait.arm()
    wait.disarm()
    expect(reconnectingRef.current).toBe(false)
    vi.advanceTimersByTime(RECONNECT_SNAPSHOT_WAIT_MS)
    wait.onSlots([])
    expect(dispatch).not.toHaveBeenCalled()
  })

  it('a read of an earlier socket cannot end the next socket\'s window', async () => {
    const { reads, reconnectingRef, wait } = harness()
    wait.arm()
    wait.onSlots([])
    wait.arm()
    reads[0].answer()
    await reads[0].settled
    expect(reconnectingRef.current).toBe(true)
    wait.onSlots([{ key: 'a' }])
    expect(reconnectingRef.current).toBe(false)
  })
})
