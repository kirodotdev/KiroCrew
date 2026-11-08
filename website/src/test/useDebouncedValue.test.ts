/** `useDebouncedValue` (`hooks/useDebouncedValue.ts`). */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

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
})
