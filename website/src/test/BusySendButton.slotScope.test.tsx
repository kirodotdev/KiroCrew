import { describe, it, expect, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useBusySendMode, readBusySendMode, BUSY_SEND_MODE_LS_KEY } from '../components/BusySendButton'

/**
 * Per-slot scoping of the busy-send (Steer/Queue) preference.
 *
 * The mode is a PER-SLOT preference: composers sharing one slot (main chat and
 * its side panel) move together, while composers bound to other slots keep
 * their own mode. A slot-less consumer keeps the unscoped legacy key.
 */
describe('useBusySendMode per-slot scoping', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('changing mode for one slot does not affect another slot', () => {
    const a = renderHook(() => useBusySendMode('slot-a'))
    const b = renderHook(() => useBusySendMode('slot-b'))
    expect(a.result.current[0]).toBe('steer')
    expect(b.result.current[0]).toBe('steer')

    act(() => { a.result.current[1]('queue') })

    expect(a.result.current[0]).toBe('queue')
    // The other slot's live mode AND stored value are untouched.
    expect(b.result.current[0]).toBe('steer')
    expect(localStorage.getItem(`${BUSY_SEND_MODE_LS_KEY}:slot-a`)).toBe('queue')
    expect(localStorage.getItem(`${BUSY_SEND_MODE_LS_KEY}:slot-b`)).toBeNull()
    expect(readBusySendMode('slot-b')).toBe('steer')
  })

  it('broadcasts within one slot: two consumers of the same slot move together', () => {
    const main = renderHook(() => useBusySendMode('slot-a'))
    const side = renderHook(() => useBusySendMode('slot-a'))

    act(() => { main.result.current[1]('queue') })

    expect(main.result.current[0]).toBe('queue')
    expect(side.result.current[0]).toBe('queue')
  })

  it('inherits the legacy unscoped value for a slot that never chose a mode', () => {
    localStorage.setItem(BUSY_SEND_MODE_LS_KEY, 'queue')
    const { result } = renderHook(() => useBusySendMode('slot-a'))
    expect(result.current[0]).toBe('queue')
    // A scoped choice then overrides the inherited legacy value for that slot only.
    act(() => { result.current[1]('steer') })
    expect(result.current[0]).toBe('steer')
    expect(readBusySendMode('slot-b')).toBe('queue')
  })

  it('never writes the unscoped legacy key: a slot-less consumer uses a scoped sentinel', () => {
    const { result } = renderHook(() => useBusySendMode(null))
    act(() => { result.current[1]('queue') })
    // The legacy key is a read-only migration source; a live write to it would
    // change the inherited default of every slot that never chose a mode.
    expect(localStorage.getItem(BUSY_SEND_MODE_LS_KEY)).toBeNull()
    expect(localStorage.getItem(`${BUSY_SEND_MODE_LS_KEY}:no-slot`)).toBe('queue')
    expect(readBusySendMode(null)).toBe('queue')
  })

  it('re-reads the mode when a mounted consumer rebinds to a different slot', () => {
    localStorage.setItem(`${BUSY_SEND_MODE_LS_KEY}:slot-a`, 'queue')
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useBusySendMode(slot),
      { initialProps: { slot: 'slot-a' } },
    )
    expect(result.current[0]).toBe('queue')

    rerender({ slot: 'slot-b' })
    expect(result.current[0]).toBe('steer')

    // After the rebind, publishes land on the NEW slot's key only.
    act(() => { result.current[1]('queue') })
    expect(localStorage.getItem(`${BUSY_SEND_MODE_LS_KEY}:slot-b`)).toBe('queue')
    expect(readBusySendMode('slot-a')).toBe('queue')
  })

  it('does not leak broadcasts to a consumer that rebound away from the slot', () => {
    const moved = renderHook(
      ({ slot }: { slot: string }) => useBusySendMode(slot),
      { initialProps: { slot: 'slot-a' } },
    )
    moved.rerender({ slot: 'slot-b' })

    const stayed = renderHook(() => useBusySendMode('slot-a'))
    act(() => { stayed.result.current[1]('queue') })

    expect(stayed.result.current[0]).toBe('queue')
    expect(moved.result.current[0]).toBe('steer')
  })
})
