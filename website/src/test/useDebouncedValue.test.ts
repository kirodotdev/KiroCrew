/** `useDebouncedValue` (`hooks/useDebouncedValue.ts`). */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useLayoutEffect } from 'react'

describe('useDebouncedValue', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('useDebouncedValue returns initial value immediately', async () => {
    const { useDebouncedValue } = await import('../hooks/useDebouncedValue')
    const { result } = renderHook(() => useDebouncedValue('hello', 200))
    expect(result.current).toBe('hello')
  })

  it('useDebouncedValue debounces updates', async () => {
    const { useDebouncedValue } = await import('../hooks/useDebouncedValue')
    const { result, rerender } = renderHook(
      ({ value }) => useDebouncedValue(value, 200),
      { initialProps: { value: 'a' } }
    )
    rerender({ value: 'b' })
    expect(result.current).toBe('a') // not yet updated
    act(() => { vi.advanceTimersByTime(200) })
    expect(result.current).toBe('b') // now updated
  })

  it('never reports a value the input has already left', async () => {
    const { useDebouncedValue } = await import('../hooks/useDebouncedValue')
    const reported: string[] = []
    const { rerender } = renderHook(
      ({ value, pumpAtCommit }) => {
        const debounced = useDebouncedValue(value, 200)
        reported.push(debounced)
        // Fires every due timer after the commit and before passive effects: the
        // window in which the timer armed for 'b' outlives the input leaving 'b'.
        useLayoutEffect(() => {
          if (pumpAtCommit) vi.advanceTimersByTime(200)
        })
        return debounced
      },
      { initialProps: { value: 'a', pumpAtCommit: false } }
    )
    rerender({ value: 'b', pumpAtCommit: false })
    rerender({ value: 'a', pumpAtCommit: true })
    act(() => { vi.advanceTimersByTime(200) })

    expect(reported).not.toContain('b')
    expect(reported.at(-1)).toBe('a')
  })
})
