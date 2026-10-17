import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useArmedDelete, ARM_REVERT_MS } from '../hooks/useArmedDelete'

/**
 * Regression guards for the shared arm→Confirm row-delete machine.
 *
 * The defect that motivated the hook: SchedulePage held the in-flight delete
 * in a single `deletingId` scalar, so a second row's confirmed delete
 * overwrote it while the first was still in flight — whichever settled first
 * then cleared the scalar, re-enabling the other still-pending row and
 * letting a re-click fire a duplicate DELETE. The same scalar's `finally`
 * also swept the armed state of WHATEVER row was armed when any delete
 * settled. These tests pin the set-keyed fixes for both, plus the machine's
 * own arm-before-delete enforcement.
 */

/** A deleteFn whose settlement the test controls, per id. */
function makeGate() {
  const resolvers = new Map<string, () => void>()
  const rejecters = new Map<string, (e: Error) => void>()
  const calls: string[] = []
  const deleteFn = vi.fn((id: string) => {
    calls.push(id)
    return new Promise<void>((resolve, reject) => {
      resolvers.set(id, resolve)
      rejecters.set(id, reject)
    })
  })
  return {
    deleteFn,
    calls,
    settle: (id: string) => act(async () => { resolvers.get(id)!() }),
    fail: (id: string) => act(async () => { rejecters.get(id)!(new Error('boom')) }),
  }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('useArmedDelete', () => {
  it('a second in-flight delete does not re-enable the first still-pending row', async () => {
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    // Arm and confirm row a, then row b while a is still in flight.
    let pA: Promise<void>
    let pB: Promise<void>
    act(() => { result.current.arm('a') })
    act(() => { pA = result.current.confirm('a') })
    act(() => { result.current.arm('b') })
    act(() => { pB = result.current.confirm('b') })
    expect(result.current.isDeleting('a')).toBe(true)
    expect(result.current.isDeleting('b')).toBe(true)

    // b settles first: a must STAY pending — the scalar version re-enabled it
    // here, which is exactly the duplicate-delete window.
    await gate.settle('b')
    await act(async () => { await pB! })
    expect(result.current.isDeleting('b')).toBe(false)
    expect(result.current.isDeleting('a')).toBe(true)
    expect(result.current.pendingIds.has('a')).toBe(true)

    await gate.settle('a')
    await act(async () => { await pA! })
    expect(result.current.isDeleting('a')).toBe(false)
    expect(result.current.pendingIds.size).toBe(0)
  })

  it('a settling delete disarms only its own row', async () => {
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    // Row a's delete is in flight; the user arms row b meanwhile.
    let pA: Promise<void>
    act(() => { result.current.arm('a') })
    act(() => { pA = result.current.confirm('a') })
    act(() => { result.current.arm('b') })
    expect(result.current.armedId).toBe('b')

    // a settling must not sweep b's armed state (the old `finally
    // { setConfirmDeleteId(null) }` did).
    await gate.settle('a')
    await act(async () => { await pA! })
    expect(result.current.armedId).toBe('b')
  })

  it('arm reverts to normal after the decay timeout', () => {
    vi.useFakeTimers()
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    act(() => { result.current.arm('a') })
    expect(result.current.armedId).toBe('a')

    act(() => { vi.advanceTimersByTime(ARM_REVERT_MS - 1) })
    expect(result.current.armedId).toBe('a')
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.armedId).toBeNull()
    expect(gate.deleteFn).not.toHaveBeenCalled()
  })

  it('re-arming restarts the decay window and moves the armed slot', () => {
    vi.useFakeTimers()
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    act(() => { result.current.arm('a') })
    act(() => { vi.advanceTimersByTime(ARM_REVERT_MS - 1) })
    // Arming b right before a's decay: one armed slot — b replaces a — and
    // the window restarts, so b must survive past a's original deadline.
    act(() => { result.current.arm('b') })
    expect(result.current.armedId).toBe('b')
    act(() => { vi.advanceTimersByTime(ARM_REVERT_MS - 1) })
    expect(result.current.armedId).toBe('b')
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.armedId).toBeNull()
  })

  it('confirm is a no-op unless the row is currently armed', async () => {
    vi.useFakeTimers()
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    // Never armed: no delete.
    await act(async () => { await result.current.confirm('a') })
    expect(gate.deleteFn).not.toHaveBeenCalled()

    // Armed but decayed: the click that lands after the revert window (with a
    // stale armed closure) must not delete — this is the safety the 3s decay
    // promises, enforced by the machine itself rather than caller wiring.
    act(() => { result.current.arm('a') })
    act(() => { vi.advanceTimersByTime(ARM_REVERT_MS) })
    expect(result.current.armedId).toBeNull()
    await act(async () => { await result.current.confirm('a') })
    expect(gate.deleteFn).not.toHaveBeenCalled()

    // A different row is armed: confirming this one is a no-op and must not
    // disturb the other row's armed state.
    act(() => { result.current.arm('b') })
    await act(async () => { await result.current.confirm('a') })
    expect(gate.deleteFn).not.toHaveBeenCalled()
    expect(result.current.armedId).toBe('b')
  })

  it('confirm is a no-op while the same row is already pending (no duplicate request)', async () => {
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    let pA: Promise<void>
    act(() => { result.current.arm('a') })
    act(() => { pA = result.current.confirm('a') })
    // A re-click on the same pending row must not fire a second request —
    // this holds even if the caller's `disabled` wiring lapses.
    await act(async () => { await result.current.confirm('a') })
    expect(gate.calls).toEqual(['a'])

    await gate.settle('a')
    await act(async () => { await pA! })
    expect(result.current.isDeleting('a')).toBe(false)
  })

  it('arm is a no-op while the same row is already pending', async () => {
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    let pA: Promise<void>
    act(() => { result.current.arm('a') })
    act(() => { pA = result.current.confirm('a') })
    // Arming a deleting row would paint a live confirm label whose second
    // click does nothing for the whole decay window.
    act(() => { result.current.arm('a') })
    expect(result.current.armedId).toBeNull()

    await gate.settle('a')
    await act(async () => { await pA! })
  })

  it('confirming disarms its own row and cancels the decay timer', async () => {
    vi.useFakeTimers()
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    act(() => { result.current.arm('a') })
    let pA: Promise<void>
    act(() => { pA = result.current.confirm('a') })
    // Armed state clears immediately (the button shows pending, not armed) …
    expect(result.current.armedId).toBeNull()
    expect(result.current.isDeleting('a')).toBe(true)

    // … and a's dead timer must not fire later to disarm a row armed after it.
    act(() => { result.current.arm('b') })
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.armedId).toBe('b')

    await gate.settle('a')
    await act(async () => { await pA! })
  })

  it('resolves (never rejects) and clears pending when deleteFn rejects', async () => {
    const gate = makeGate()
    const { result } = renderHook(() => useArmedDelete(gate.deleteFn))

    let pA: Promise<void>
    act(() => { result.current.arm('a') })
    act(() => { pA = result.current.confirm('a') })
    // confirm's contract: deleteFn owns its error surfacing, so the promise
    // handed back to an event handler must never reject.
    const outcome = pA!.then(() => 'resolved', () => 'rejected')
    expect(result.current.isDeleting('a')).toBe(true)

    await gate.fail('a')
    await act(async () => { expect(await outcome).toBe('resolved') })
    // The row must come back deletable after a failure, never stuck pending.
    expect(result.current.isDeleting('a')).toBe(false)
    expect(result.current.pendingIds.size).toBe(0)
  })
})
