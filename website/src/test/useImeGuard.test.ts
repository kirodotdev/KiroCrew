import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useImeGuard } from '../hooks/useImeGuard'

// Minimal KeyboardEvent shape the hook reads.
const key = (opts: { isComposing?: boolean; keyCode?: number } = {}) =>
  ({ nativeEvent: { isComposing: opts.isComposing ?? false }, keyCode: opts.keyCode ?? 13 }) as
    unknown as React.KeyboardEvent

describe('useImeGuard', () => {
  it('blocks while composition is active (composingRef)', () => {
    const { result } = renderHook(() => useImeGuard())
    act(() => result.current.onCompositionStart())
    expect(result.current.isComposing(key())).toBe(true)
  })

  it('blocks when e.nativeEvent.isComposing is true', () => {
    const { result } = renderHook(() => useImeGuard())
    expect(result.current.isComposing(key({ isComposing: true }))).toBe(true)
  })

  it('blocks when e.keyCode === 229 (IME processing)', () => {
    const { result } = renderHook(() => useImeGuard())
    expect(result.current.isComposing(key({ keyCode: 229 }))).toBe(true)
  })

  it('blocks for 50ms after compositionEnd, unblocks after', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderHook(() => useImeGuard())
      act(() => {
        result.current.onCompositionStart()
        result.current.onCompositionEnd()
      })
      expect(result.current.isComposing(key())).toBe(true)
      act(() => { vi.advanceTimersByTime(50) })
      expect(result.current.isComposing(key())).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears stale timer on new compositionStart (back-to-back IME sequences)', () => {
    vi.useFakeTimers()
    try {
      const { result } = renderHook(() => useImeGuard())
      // First composition ends — schedules a 50ms timer
      act(() => {
        result.current.onCompositionStart()
        result.current.onCompositionEnd()
      })
      // Second composition starts within 50ms — should clear the stale timer
      act(() => { vi.advanceTimersByTime(20) })
      act(() => { result.current.onCompositionStart() })
      // Let stale timer's original 50ms window fully elapse
      act(() => { vi.advanceTimersByTime(100) })
      // composingRef must still be true — the stale timer was cleared
      expect(result.current.isComposing(key())).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('reset() clears composingRef (for unmount/Escape paths in shared-instance scenarios)', () => {
    const { result } = renderHook(() => useImeGuard())
    act(() => result.current.onCompositionStart())
    expect(result.current.isComposing(key())).toBe(true)
    act(() => result.current.reset())
    expect(result.current.isComposing(key())).toBe(false)
  })

  it('the bare composition binding carries ONLY the composition handlers', () => {
    // Pins the docblock's claim: `composition` does not auto-reset. A consumer that
    // needs abandoned-composition recovery wires `reset()` itself, or consumes
    // `useComposerDraft`, whose composition binding adds the blur reset. Adding a
    // handler here changes every `{...ime.composition}` spread in the tree — do it
    // deliberately, with the consumer audit, not by accident.
    const { result } = renderHook(() => useImeGuard())
    expect(Object.keys(result.current.composition).sort())
      .toEqual(['onCompositionEnd', 'onCompositionStart'])
  })

  it('clears pending timer on unmount (no stale timer callbacks after teardown)', () => {
    vi.useFakeTimers()
    const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout')
    try {
      const { result, unmount } = renderHook(() => useImeGuard())
      act(() => {
        result.current.onCompositionStart()
        result.current.onCompositionEnd()
      })
      // A 50ms timer is now pending.
      const callsBeforeUnmount = clearTimeoutSpy.mock.calls.length
      unmount()
      // The useEffect cleanup must have cleared the pending timer on unmount.
      expect(clearTimeoutSpy.mock.calls.length).toBeGreaterThan(callsBeforeUnmount)
    } finally {
      clearTimeoutSpy.mockRestore()
      vi.useRealTimers()
    }
  })

  describe('bindEnter', () => {
    it('onBlur resets composingRef AND forwards user callback', () => {
      const onBlur = vi.fn()
      const { result } = renderHook(() => useImeGuard())
      act(() => result.current.onCompositionStart())
      expect(result.current.isComposing(key())).toBe(true)

      const props = result.current.bindEnter<HTMLInputElement>({ onBlur })
      act(() => props.onBlur({} as React.FocusEvent<HTMLInputElement>))

      // reset() cleared composingRef
      expect(result.current.isComposing(key())).toBe(false)
      // user callback still invoked
      expect(onBlur).toHaveBeenCalledTimes(1)
    })

    it('Escape resets composingRef BEFORE invoking onEscape (order matters)', () => {
      const { result } = renderHook(() => useImeGuard())
      const seen: boolean[] = []
      const onEscape = vi.fn(() => {
        // When onEscape runs, composingRef must already be cleared.
        seen.push(result.current.isComposing(key()))
      })
      act(() => result.current.onCompositionStart())
      expect(result.current.isComposing(key())).toBe(true)

      const props = result.current.bindEnter<HTMLInputElement>({ onEscape })
      act(() => props.onKeyDown({
        key: 'Escape',
        preventDefault: vi.fn(),
        nativeEvent: { isComposing: false },
        keyCode: 27,
      } as unknown as React.KeyboardEvent<HTMLInputElement>))

      expect(onEscape).toHaveBeenCalledTimes(1)
      expect(seen).toEqual([false])
    })

    it('Enter calls preventDefault and invokes onEnter when not composing', () => {
      const onEnter = vi.fn()
      const preventDefault = vi.fn()
      const { result } = renderHook(() => useImeGuard())
      const props = result.current.bindEnter<HTMLInputElement>({ onEnter })

      act(() => props.onKeyDown({
        key: 'Enter',
        preventDefault,
        nativeEvent: { isComposing: false },
        keyCode: 13,
      } as unknown as React.KeyboardEvent<HTMLInputElement>))

      expect(preventDefault).toHaveBeenCalledTimes(1)
      expect(onEnter).toHaveBeenCalledTimes(1)
    })

    it('Enter during composition does NOT preventDefault or invoke onEnter', () => {
      const onEnter = vi.fn()
      const preventDefault = vi.fn()
      const { result } = renderHook(() => useImeGuard())
      act(() => result.current.onCompositionStart())
      const props = result.current.bindEnter<HTMLInputElement>({ onEnter })

      act(() => props.onKeyDown({
        key: 'Enter',
        preventDefault,
        nativeEvent: { isComposing: false },
        keyCode: 13,
      } as unknown as React.KeyboardEvent<HTMLInputElement>))

      expect(preventDefault).not.toHaveBeenCalled()
      expect(onEnter).not.toHaveBeenCalled()
    })
  })
})
