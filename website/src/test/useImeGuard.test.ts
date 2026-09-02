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

  it('the composition binding carries the latch recovery, and composes caller handlers', () => {
    // There is no recovery-less binding to pick. A composition abandoned without a
    // `compositionend` latches the guard, and since claimEnter consumes what it
    // declines, a surface missing the reset stops sending SILENTLY. Shipping the reset
    // with the tracking is what makes that unreachable rather than merely documented.
    // A caller's own blur/focus handler is composed, never replaced — the earlier shape,
    // where a consumer spread the binding and then declared its own `onBlur`, dropped
    // the reset without a word.
    const { result } = renderHook(() => useImeGuard())
    expect(Object.keys(result.current.bindComposition()).sort())
      .toEqual(['onBlur', 'onCompositionEnd', 'onCompositionStart', 'onFocus'])

    const onBlur = vi.fn()
    const bound = result.current.bindComposition<HTMLTextAreaElement>({ onBlur })
    act(() => result.current.onCompositionStart())
    expect(result.current.isComposing(key())).toBe(true)
    act(() => bound.onBlur({} as React.FocusEvent<HTMLTextAreaElement>))
    expect(result.current.isComposing(key())).toBe(false)
    expect(onBlur).toHaveBeenCalledTimes(1)
  })

  it('the composition binding resets a stale latch on focus, and composes a caller onFocus', () => {
    // One hook instance is routinely shared across sibling inputs. A latch stranded
    // by the previous element (composition abandoned mid-flight) must not decline
    // the first Enter on the next one, so the reset rides in the binding's onFocus
    // — the site-level `onFocus={() => ime.reset()}` copies this replaces could be
    // dropped by omission on a new consumer.
    const { result } = renderHook(() => useImeGuard())
    const onFocus = vi.fn()
    const bound = result.current.bindComposition<HTMLInputElement>({ onFocus })
    act(() => result.current.onCompositionStart())
    expect(result.current.isComposing(key())).toBe(true)
    act(() => bound.onFocus({} as React.FocusEvent<HTMLInputElement>))
    expect(result.current.isComposing(key())).toBe(false)
    expect(onFocus).toHaveBeenCalledTimes(1)
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

    it('onFocus resets a stale latch AND forwards user callback', () => {
      const onFocus = vi.fn()
      const { result } = renderHook(() => useImeGuard())
      act(() => result.current.onCompositionStart())
      expect(result.current.isComposing(key())).toBe(true)

      const props = result.current.bindEnter<HTMLInputElement>({ onFocus })
      act(() => props.onFocus({} as React.FocusEvent<HTMLInputElement>))

      expect(result.current.isComposing(key())).toBe(false)
      expect(onFocus).toHaveBeenCalledTimes(1)
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

    it('Enter during composition is CONSUMED but does not invoke onEnter', () => {
      // Not submitting is not the same as declining the key. `bindEnter` also serves
      // multiline inputs, where an Enter left to the browser inserts a newline into
      // the value the user is about to commit — so the swallow must still consume it.
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

      expect(preventDefault).toHaveBeenCalledTimes(1)
      expect(onEnter).not.toHaveBeenCalled()
    })
  })

  describe('claimEnter', () => {
    const claimKey = (opts: { isComposing?: boolean; keyCode?: number } = {}) => {
      const preventDefault = vi.fn()
      const e = {
        preventDefault,
        nativeEvent: { isComposing: opts.isComposing ?? false },
        keyCode: opts.keyCode ?? 13,
      } as unknown as React.KeyboardEvent
      return { e, preventDefault }
    }

    it('claims the key and reports true when no composition is in flight', () => {
      const { result } = renderHook(() => useImeGuard())
      const { e, preventDefault } = claimKey()
      expect(result.current.claimEnter(e)).toBe(true)
      expect(preventDefault).toHaveBeenCalledTimes(1)
    })

    it('claims the key and reports false while composing', () => {
      const { result } = renderHook(() => useImeGuard())
      act(() => result.current.onCompositionStart())
      const { e, preventDefault } = claimKey()
      expect(result.current.claimEnter(e)).toBe(false)
      // The whole point: a swallowed Enter is consumed, not handed to the browser.
      expect(preventDefault).toHaveBeenCalledTimes(1)
    })

    it('leaves the default alone when a native signal reports composing', () => {
      // The browser is consuming this keypress for the IME, so there is no newline
      // to prevent — and the same press carries the candidate commit, which is not
      // ours to cancel. Both native signals are checked; the tracked latch is not,
      // because the latch outliving them is exactly the case that DOES need claiming.
      const { result } = renderHook(() => useImeGuard())
      for (const opts of [{ isComposing: true }, { keyCode: 229 }]) {
        const { e, preventDefault } = claimKey(opts)
        expect(result.current.claimEnter(e)).toBe(false)
        expect(preventDefault).not.toHaveBeenCalled()
      }
    })

    it('claims the key inside the post-compositionEnd window', () => {
      // The window is the guard's own false-positive surface: a fast typist who picks a
      // candidate and presses Enter lands in it on a browser that never needed the
      // timer. Swallowing there is acceptable; leaking a newline into the draft is not.
      vi.useFakeTimers()
      try {
        const { result } = renderHook(() => useImeGuard())
        act(() => {
          result.current.onCompositionStart()
          result.current.onCompositionEnd()
        })
        const inWindow = claimKey()
        expect(result.current.claimEnter(inWindow.e)).toBe(false)
        expect(inWindow.preventDefault).toHaveBeenCalledTimes(1)

        act(() => { vi.advanceTimersByTime(50) })
        const after = claimKey()
        expect(result.current.claimEnter(after.e)).toBe(true)
        expect(after.preventDefault).toHaveBeenCalledTimes(1)
      } finally {
        vi.useRealTimers()
      }
    })
  })

  describe('claimKey (synthetic delegate onto the instance latch)', () => {
    // The delegate hands e.nativeEvent to ImeLatch.claimKey, so consumption
    // happens on the NATIVE half: stopPropagation always on a decline,
    // preventDefault only when both native signals are clear (the tracked
    // latch window). The delegate itself stops the SYNTHETIC propagation on
    // a decline — React walks its own flag for component ancestors, which
    // the native call does not set. An ACCEPTED key is left untouched —
    // unlike claimEnter, the caller consumes it as part of acting (a trap's
    // own preventDefault).
    const tabKey = (opts: { isComposing?: boolean; keyCode?: number } = {}) => {
      const preventDefault = vi.fn()
      const stopPropagation = vi.fn()
      const syntheticStopPropagation = vi.fn()
      const e = {
        stopPropagation: syntheticStopPropagation,
        nativeEvent: {
          isComposing: opts.isComposing ?? false,
          keyCode: opts.keyCode ?? 9,
          preventDefault,
          stopPropagation,
        },
      } as unknown as React.KeyboardEvent
      return { e, preventDefault, stopPropagation, syntheticStopPropagation }
    }

    it('reports true and leaves the key untouched when no composition is in flight', () => {
      const { result } = renderHook(() => useImeGuard())
      const { e, preventDefault, stopPropagation, syntheticStopPropagation } = tabKey()
      expect(result.current.claimKey(e)).toBe(true)
      expect(preventDefault).not.toHaveBeenCalled()
      expect(stopPropagation).not.toHaveBeenCalled()
      expect(syntheticStopPropagation).not.toHaveBeenCalled()
    })

    it('declines and consumes inside the tracked-latch window (native signals clear)', () => {
      // The WebKit hazard: the committing keydown arrives after compositionend
      // with isComposing already false, so only the latch can see it — and the
      // browser WOULD act on it, so the decline must own both halves.
      vi.useFakeTimers()
      try {
        const { result } = renderHook(() => useImeGuard())
        act(() => {
          result.current.onCompositionStart()
          result.current.onCompositionEnd()
        })
        const { e, preventDefault, stopPropagation, syntheticStopPropagation } = tabKey()
        expect(result.current.claimKey(e)).toBe(false)
        expect(preventDefault).toHaveBeenCalledTimes(1)
        expect(stopPropagation).toHaveBeenCalledTimes(1)
        expect(syntheticStopPropagation).toHaveBeenCalledTimes(1)

        act(() => { vi.advanceTimersByTime(50) })
        const after = tabKey()
        expect(result.current.claimKey(after.e)).toBe(true)
        expect(after.preventDefault).not.toHaveBeenCalled()
      } finally {
        vi.useRealTimers()
      }
    })

    it('declines a mid-composition key without cancelling its default action', () => {
      // A native signal set means the browser is consuming the key for the
      // IME itself (candidate navigation, or the commit) — cancelling that
      // would eat the user's composition. Propagation still stops on BOTH
      // halves: the key is not the caller's, and not any ancestor's either.
      const { result } = renderHook(() => useImeGuard())
      for (const opts of [{ isComposing: true }, { keyCode: 229 }]) {
        const { e, preventDefault, stopPropagation, syntheticStopPropagation } = tabKey(opts)
        expect(result.current.claimKey(e)).toBe(false)
        expect(preventDefault).not.toHaveBeenCalled()
        expect(stopPropagation).toHaveBeenCalledTimes(1)
        expect(syntheticStopPropagation).toHaveBeenCalledTimes(1)
      }
    })
  })
})
