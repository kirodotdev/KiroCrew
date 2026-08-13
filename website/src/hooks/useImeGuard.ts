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
 * `composingRef` would stay true forever, blocking Enter on every input that
 * shares this hook. Recovery therefore ships WITH the tracking: the only
 * composition binding this hook exposes carries the blur reset, so a surface
 * cannot opt out of it by omission. That matters more now than it used to,
 * because `claimEnter` also consumes the keypresses a latched guard declines —
 * the failure mode is a surface that silently stops sending rather than one
 * that visibly inserts newlines.
 *
 * **A swallowed Enter still has to be consumed.** Deciding not to submit is not
 * the same as declining the keypress: on a multiline input the browser's default
 * for an Enter nobody claimed is to insert a literal newline, so a guard that
 * returns early without `preventDefault` turns "this Enter belongs to the IME"
 * into "corrupt the user's draft". `claimEnter` exists so no call site has to
 * remember that. It suppresses the default only where the browser would
 * otherwise act — both native signals clear — and leaves a keypress the IME is
 * itself consuming alone. The window this matters in is not hypothetical: the
 * `composingRef` timer above outlives the native signals by 50ms, and a fast
 * typist's send lands inside it.
 *
 * Usage (simple Enter/Escape inputs):
 *   const ime = useImeGuard()
 *   <input {...ime.bindEnter({ onEnter: submit, onEscape: cancel, onBlur: commit })} />
 *
 * Usage (custom onKeyDown logic):
 *   <textarea
 *     {...ime.bindComposition()}
 *     onKeyDown={e => {
 *       if (e.key === 'Enter' && !e.shiftKey) { if (ime.claimEnter(e)) submit(); return }
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

  /**
   * Take ownership of an Enter the caller has already decided is a submit key,
   * and report whether to act on it. `true` = submit, `false` = the IME is
   * committing a candidate, so do nothing.
   *
   * The default action is suppressed only when the browser would otherwise act
   * on the key, which is exactly when both native signals read "not composing".
   * That split matters in both directions:
   *
   *   - Native signal set: the browser is consuming the key for the IME itself,
   *     so there is no newline to prevent, and cancelling its default action
   *     risks the candidate commit that the same keypress carries.
   *   - Latch only: the browser considers the composition finished (the window
   *     past `compositionend`, or one abandoned without it) and will insert a
   *     line break into the draft. Nothing live is cancelled by claiming it.
   *
   * Returning a boolean rather than leaving `preventDefault` to the caller is
   * the point: the guard's negative answer means "not a submit", and every call
   * site got the second half wrong in the same way.
   */
  const claimEnter = (e: KeyboardEvent) => {
    if (!e.nativeEvent.isComposing && e.keyCode !== 229) e.preventDefault()
    return !isComposing(e)
  }

  /**
   * Spread onto any input or textarea that needs IME-safe composition tracking.
   *
   * The blur reset is not optional and not the caller's to remember. A composition
   * abandoned WITHOUT a `compositionend` — focus moves away mid-composition, the
   * element unmounts, an OS-level IME cancel — leaves `composingRef` latched, and a
   * latched guard declines every later Enter for the element's lifetime. Since
   * `claimEnter` also consumes those keypresses, a latched guard is SILENT: the
   * surface simply stops sending. So the recovery ships with the tracking, and a
   * caller's own blur handler is composed rather than replacing it.
   */
  const bindComposition = <T extends HTMLElement>(opts: {
    onBlur?: (e: FocusEvent<T>) => void
  } = {}) => ({
    onCompositionStart,
    onCompositionEnd,
    onBlur: (e: FocusEvent<T>) => { reset(); opts.onBlur?.(e) },
  })

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
    ...bindComposition<T>({ onBlur: opts.onBlur }),
    onKeyDown: (e: KeyboardEvent<T>) => {
      if (e.key === 'Enter' && claimEnter(e)) opts.onEnter?.()
      if (e.key === 'Escape') { reset(); opts.onEscape?.() }
    },
  })

  return { onCompositionStart, onCompositionEnd, isComposing, claimEnter, reset, bindComposition, bindEnter }
}
