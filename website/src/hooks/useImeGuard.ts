import { useRef, useEffect, type KeyboardEvent, type FocusEvent } from 'react'

/**
 * Guard against IME composition Enter falsely triggering submit handlers.
 *
 * IME (Chinese/Japanese/Korean) sends a final Enter to commit the composition.
 * React's synthetic `isComposing` is sometimes false on that final Enter, so
 * this hook layers multiple guards:
 *
 *   1. `composingRef`              - true from compositionStart until 50ms
 *                                    after compositionEnd (timer-based)
 *   2. `e.nativeEvent.isComposing` - native browser flag
 *   3. `e.keyCode === 229`         - "IME processing" keyCode some browsers
 *                                    emit while composition is in flight even
 *                                    after isComposing flips to false
 *
 * The 50ms guard is tracked via `setTimeout` whose handle is cleared on every
 * new `compositionStart` - prevents a stale timer from flipping composingRef
 * back to false while a follow-up (back-to-back) composition is mid-flight.
 *
 * **Sharing a single hook instance across multiple inputs:** If the hosting
 * component unmounts an input mid-composition (e.g. Escape cancels a rename
 * and removes the input from the tree), `compositionEnd` will never fire and
 * `composingRef` would stay true forever, blocking Enter on other inputs that
 * share this hook. `bindEnter` and `composition` auto-`reset()` on blur/Escape
 * to avoid that.
 *
 * Usage (simple Enter/Escape inputs):
 *   const ime = useImeGuard()
 *   <input {...ime.bindEnter({ onEnter: submit, onEscape: cancel, onBlur: commit })} />
 *
 * Usage (custom onKeyDown logic):
 *   <textarea
 *     {...ime.composition}
 *     onKeyDown={e => {
 *       if (e.key === 'Enter' && !ime.isComposing(e)) { ... }
 *       if (e.key === 'Escape') { ime.reset(); ... }
 *     }}
 *   />
 */
export function useImeGuard() {
  const composingRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout>>()

  // Clear any pending post-composition timer when the host component unmounts.
  // Prevents stale timer callbacks from writing to the ref after teardown.
  useEffect(() => () => { clearTimeout(timerRef.current) }, [])

  const reset = () => {
    clearTimeout(timerRef.current)
    composingRef.current = false
  }

  const onCompositionStart = () => {
    clearTimeout(timerRef.current)
    composingRef.current = true
  }
  const onCompositionEnd = () => {
    composingRef.current = true
    timerRef.current = setTimeout(() => { composingRef.current = false }, 50)
  }
  const isComposing = (e: KeyboardEvent) =>
    composingRef.current || e.nativeEvent.isComposing || e.keyCode === 229

  /** Spread onto any input/textarea that needs IME-safe composition tracking. */
  const composition = { onCompositionStart, onCompositionEnd }

  /**
   * Spread onto simple Enter-to-submit / Escape-to-cancel inputs. Auto-resets
   * stale composition state on blur & Escape so sharing one hook instance
   * across sibling inputs is safe.
   */
  const bindEnter = <T extends HTMLElement>(opts: {
    onEnter?: () => void
    onEscape?: () => void
    onBlur?: (e: FocusEvent<T>) => void
  }) => ({
    ...composition,
    onBlur: (e: FocusEvent<T>) => { reset(); opts.onBlur?.(e) },
    onKeyDown: (e: KeyboardEvent<T>) => {
      if (e.key === 'Enter' && !isComposing(e)) { e.preventDefault(); opts.onEnter?.() }
      if (e.key === 'Escape') { reset(); opts.onEscape?.() }
    },
  })

  return { onCompositionStart, onCompositionEnd, isComposing, reset, composition, bindEnter }
}
