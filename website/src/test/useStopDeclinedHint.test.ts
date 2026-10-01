import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useStopDeclinedHint } from '../hooks/useStopDeclinedHint'

describe('useStopDeclinedHint', () => {
  it('arms on a decline and expires on its own after the window', () => {
    vi.useFakeTimers()
    try {
      const { result, rerender } = renderHook(({ d }) => useStopDeclinedHint(d, 1000), {
        initialProps: { d: false },
      })
      expect(result.current).toBe(false)
      rerender({ d: true })
      expect(result.current).toBe(true)
      // The backend pushes no frame at expiry; the hint must still go away.
      act(() => { vi.advanceTimersByTime(1001) })
      expect(result.current).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('drops as soon as the backend reports the marker cleared', () => {
    const { result, rerender } = renderHook(({ d }) => useStopDeclinedHint(d, 60_000), {
      initialProps: { d: true },
    })
    expect(result.current).toBe(true)
    rerender({ d: false })
    expect(result.current).toBe(false)
  })
})
