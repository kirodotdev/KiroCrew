import { useRef, useEffect, type KeyboardEvent, type FocusEvent } from 'react'

/**
 * How long after `compositionend` an Enter keydown still belongs to the IME.
 * On WebKit the keydown that commits a candidate arrives AFTER
 * `compositionend` with `isComposing` already false, so the native flags alone
 * cannot identify it — the tracked latch below outlives them by this window.
 */
const POST_COMPOSITION_MS = 50

export interface ImeLatch {
  onCompositionStart: () => void
  onCompositionEnd: () => void
  /** True while a composition is live or inside the post-composition window. */
  isLatched: () => boolean
  /** Clear the latch and any pending timer (stale-latch recovery). */
  reset: () => void
  /**
   * Native-event twin of `useImeGuard().claimEnter`: take ownership of a key
   * the caller has decided is a choose/submit key, and report whether to act.
   * `true` = act on it (the caller consumes it as part of acting). `false` =
   * the IME owns this keypress; the claim has already consumed it — always
   * `stopPropagation()` (so it cannot fall through to the host composer), and
   * `preventDefault()` only when both native signals are clear (the
   * post-composition latch window, where the browser would otherwise act).
   * A mid-composition key keeps its default action: the browser is consuming
   * it for the candidate commit or candidate navigation itself, and
   * cancelling that would eat the user's composition. Owning both halves in
   * one place is the point — a call site re-spelling the split is the drift
   * the `ImeEnterClaimRatchet` exists to prevent.
   */
  claimKey: (e: globalThis.KeyboardEvent) => boolean
}

/**
 * The tracked half of the IME guard, framework-free so both event worlds share
 * ONE latch spelling: `useImeGuard` layers it under React synthetic events, and
 * native-event listeners (document-level keydown handlers, which never see a
 * synthetic event and therefore cannot call `claimEnter`) consume it directly —
 * `useListKeyboardNav` is the reference consumer. A private flag-and-timer copy
 * in a native handler is exactly the drift the `ImeEnterClaimRatchet` pins.
 *
 * `latched` stays true from `compositionstart` until POST_COMPOSITION_MS after
 * `compositionend`. The timer handle is cleared on every new
 * `compositionstart`, so a stale timer cannot flip the latch back to false
 * while a follow-up (back-to-back) composition is mid-flight.
 */
export function createImeLatch(): ImeLatch {
  let latched = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const isLatched = () => latched
  return {
    onCompositionStart() {
      clearTimeout(timer)
      latched = true
    },
    onCompositionEnd() {
      latched = true
      timer = setTimeout(() => { latched = false }, POST_COMPOSITION_MS)
    },
    isLatched,
    reset() {
      clearTimeout(timer)
      latched = false
    },
    claimKey(e: globalThis.KeyboardEvent) {
      const composing = latched || e.isComposing || e.keyCode === 229
      if (!composing) return true
      if (!e.isComposing && e.keyCode !== 229) e.preventDefault()
      e.stopPropagation()
      return false
    },
  }
}

/**
 * Document-tracked IME latch for NATIVE keydown handlers that trap Tab at a
 * dialog's focus boundary (`useDialogFocusTrap` and the hand-rolled traps that
 * share its shape). Those listeners receive native KeyboardEvents, which the
 * synthetic-only `useImeGuard().claimEnter` cannot consume — so they share the
 * tracked latch through this hook instead of each re-implementing the
 * flag-and-timer semantics (the drift the `ImeEnterClaimRatchet` pins).
 *
 * Composition tracking listens at DOCUMENT capture: the composing input is
 * anywhere inside the dialog, so element-scoped listeners have nothing
 * reliable to attach to.
 *
 * `enabled` keys the tracking lifecycle and nothing else, so the latch
 * survives the host's own re-renders: the commit's input event re-renders the
 * host right before the committing keydown arrives, and a latch reset riding
 * along on that churn would clear the post-composition window at exactly the
 * moment it matters. A re-enabled latch does NOT inherit state from its
 * disabled span — a composition that ended (or was abandoned) while the latch
 * was off would otherwise strand it.
 *
 * Stranded-latch recovery ships with the tracking: a composition abandoned
 * WITHOUT `compositionend` (focus moves off the input mid-composition, an
 * OS-level IME cancel) would otherwise leave the latch set — and a latched
 * guard consumes the keys it declines, so the trap would silently stop
 * cycling for the dialog's lifetime. `focusout` at document capture is the
 * recovery signal.
 *
 * The returned latch is identity-stable for the host's lifetime (it lives in
 * a ref), so listing it in an effect dependency array never re-runs the
 * effect.
 */
export function useDocumentImeLatch(enabled = true): ImeLatch {
  const latchRef = useRef<ImeLatch>()
  if (!latchRef.current) latchRef.current = createImeLatch()
  const latch = latchRef.current
  useEffect(() => {
    if (!enabled) return
    latch.reset()
    const onCompositionStart = () => latch.onCompositionStart()
    const onCompositionEnd = () => latch.onCompositionEnd()
    const onRecover = () => latch.reset()
    document.addEventListener('compositionstart', onCompositionStart, true)
    document.addEventListener('compositionend', onCompositionEnd, true)
    document.addEventListener('focusout', onRecover, true)
    return () => {
      document.removeEventListener('compositionstart', onCompositionStart, true)
      document.removeEventListener('compositionend', onCompositionEnd, true)
      document.removeEventListener('focusout', onRecover, true)
      // Drop any pending post-composition timer with the listeners so a timer
      // from a disabled span cannot fire into the next enablement.
      latch.reset()
    }
  }, [enabled, latch])
  return latch
}

/**
 * Guard against IME composition Enter falsely triggering submit handlers.
 *
 * IME (Chinese/Japanese/Korean) sends a final Enter to commit the composition.
 * React's synthetic `isComposing` is sometimes false on that final Enter, so
 * this hook layers multiple guards:
 *
 *   1. the tracked latch (`createImeLatch`) - true from compositionStart until
 *                                    50ms after compositionEnd (timer-based)
 *   2. `e.nativeEvent.isComposing` - native browser flag
 *   3. `e.keyCode === 229`         - "IME processing" keyCode some browsers
 *                                    emit while composition is in flight even
 *                                    after isComposing flips to false
 *
 * **Sharing a single hook instance across multiple inputs:** If the hosting
 * component unmounts an input mid-composition (e.g. Escape cancels a rename
 * and removes the input from the tree), `compositionEnd` will never fire and
 * the latch would stay set forever, blocking Enter on every input that
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
 * latch timer above outlives the native signals by 50ms, and a fast
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
  const latchRef = useRef<ImeLatch>()
  if (!latchRef.current) latchRef.current = createImeLatch()
  const latch = latchRef.current

  // Clear any pending post-composition timer when the host component unmounts.
  // Prevents stale timer callbacks from writing to the latch after teardown.
  useEffect(() => () => { latch.reset() }, [latch])

  const reset = () => latch.reset()

  const onCompositionStart = () => latch.onCompositionStart()
  const onCompositionEnd = () => latch.onCompositionEnd()
  const isComposing = (e: KeyboardEvent) =>
    latch.isLatched() || e.nativeEvent.isComposing || e.keyCode === 229

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
   * Synthetic-event twin of `ImeLatch.claimKey`, for choose-class keys that
   * are NOT Enter (a boundary Tab that would cycle focus, an Escape that
   * would dismiss): it claims through this instance's latch, so the decline
   * owns both halves exactly as the native contract specifies — always
   * `stopPropagation`, `preventDefault` only in the post-composition window
   * where the browser would otherwise act. The latch consumes the NATIVE
   * event; the SYNTHETIC propagation flag is stopped here too, because React
   * walks its own flag when dispatching to component ancestors and the
   * native call does not set it — leaving that to the caller would re-open
   * the split-the-halves drift this file's ratchet exists to prevent. Enter
   * branches keep using `claimEnter`, whose ACCEPTED path also consumes the
   * key (a submitted Enter must never insert a newline); `claimKey` leaves an
   * accepted key's default to the caller, which is what a Tab site needs —
   * it consumes only the wrap it owns.
   */
  const claimKey = (e: KeyboardEvent) => {
    if (latch.claimKey(e.nativeEvent)) return true
    e.stopPropagation()
    return false
  }

  /**
   * Spread onto any input or textarea that needs IME-safe composition tracking.
   *
   * The blur reset is not optional and not the caller's to remember. A composition
   * abandoned WITHOUT a `compositionend` — focus moves away mid-composition, the
   * element unmounts, an OS-level IME cancel — leaves the latch set, and a
   * latched guard declines every later Enter for the element's lifetime. Since
   * `claimEnter` also consumes those keypresses, a latched guard is SILENT: the
   * surface simply stops sending. So the recovery ships with the tracking, and a
   * caller's own blur handler is composed rather than replacing it.
   *
   * The focus reset is the other half of the same recovery: when one hook
   * instance is shared across sibling inputs (or an input remounts), a latch
   * stranded by the previous element must not decline the first Enter on the
   * next one. Sites used to spell `onFocus={() => ime.reset()}` by hand; the
   * binding now carries it so a consumer cannot opt out by omission, and a
   * caller's own focus handler is composed rather than replacing it.
   */
  const bindComposition = <T extends HTMLElement>(opts: {
    onFocus?: (e: FocusEvent<T>) => void
    onBlur?: (e: FocusEvent<T>) => void
  } = {}) => ({
    onCompositionStart,
    onCompositionEnd,
    onFocus: (e: FocusEvent<T>) => { reset(); opts.onFocus?.(e) },
    onBlur: (e: FocusEvent<T>) => { reset(); opts.onBlur?.(e) },
  })

  /**
   * Spread onto simple Enter-to-submit / Escape-to-cancel inputs. Auto-resets
   * stale composition state on blur & Escape so sharing one hook instance
   * across sibling inputs is safe.
   *
   * CONTRACT: single-line inputs only, and modifier keys do not participate —
   * Shift/Cmd/Ctrl+Enter all submit. A textarea (where Shift+Enter must stay a
   * soft break) or any handler that branches on modifiers keeps its own
   * `onKeyDown` with `claimEnter` in the Enter branch instead.
   */
  const bindEnter = <T extends HTMLElement>(opts: {
    onEnter?: () => void
    onEscape?: () => void
    onFocus?: (e: FocusEvent<T>) => void
    onBlur?: (e: FocusEvent<T>) => void
  }) => ({
    ...bindComposition<T>({ onFocus: opts.onFocus, onBlur: opts.onBlur }),
    onKeyDown: (e: KeyboardEvent<T>) => {
      if (e.key === 'Enter' && claimEnter(e)) opts.onEnter?.()
      if (e.key === 'Escape') { reset(); opts.onEscape?.() }
    },
  })

  return { onCompositionStart, onCompositionEnd, isComposing, claimEnter, claimKey, reset, bindComposition, bindEnter }
}
