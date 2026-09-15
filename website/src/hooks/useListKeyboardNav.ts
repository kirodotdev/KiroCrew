import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, MutableRefObject, SetStateAction } from 'react'
import { createImeLatch } from './useImeGuard'

/**
 * Shared list keyboard-navigation hook for picker-menu / palette surfaces
 * (Search Everywhere, file picker, etc).
 *
 * It owns a single document-level **capture-phase** `keydown` listener (active
 * only while `open`) that drives a roving selection over a flat list of
 * `count` rows:
 *
 *  - `ArrowDown` / `ArrowUp` — move the selection, wrapping at the ends, and
 *    scroll the newly-selected row (via {@link itemRefs}) into view.
 *  - `Enter` / `Tab`         — "choose" the selected row (`onChoose`). `Tab` is
 *    the picker-menu default; a host that needs `Tab` for something else (the
 *    palette uses it to cycle category tabs) registers its own *window*-capture
 *    listener — which runs before this document-capture one — and calls
 *    `stopImmediatePropagation()` so this handler never sees the event.
 *    Both choose keys are IME-guarded: while a composition is live or inside
 *    the post-composition latch window (shared with `useImeGuard` via
 *    `createImeLatch`), the key declines the choose and is consumed per
 *    `claimKey`'s contract instead. A host interceptor that outranks this
 *    listener bypasses that guard along with the dispatch, so it must consult
 *    the same latch through the returned {@link ListKeyboardNav.claimKey}
 *    before acting on a choose-class key.
 *  - `Alt`/`Option`+`Enter`  — `onAltEnter(selected)` when provided; if it
 *    returns `true` the event is treated as handled.
 *  - `Escape`                — `onClose()`, unless the shared IME latch owns
 *                              the key for candidate dismissal.
 *
 * The selection is mirrored into {@link selectedRef} so callbacks captured in
 * effects can read the live value without re-subscribing, matching the
 * existing FilePickerMenu pattern.
 */
export interface UseListKeyboardNavOptions {
  /** Whether the owning surface is open. The listener is only attached while true. */
  open: boolean
  /** Number of selectable rows currently rendered. */
  count: number
  /**
   * Whether ArrowDown/ArrowUp wrap around at edges (default: false).
   * Set to true for surfaces (like the palette) that should wrap around.
   */
  wrap?: boolean
  /**
   * Activate the row at `index` (Enter / Tab). `withModifier` is `true` when
   * the activating keypress held ⌘ (metaKey) or Ctrl (ctrlKey) — the palette
   * threads this into its central `dispatchEnter` to select the modifier branch
   * of the §2 Enter matrix (always-new-session / attach-as-context). Tab and a
   * bare Enter pass `false`. Callers that ignore the second argument are
   * unaffected.
   */
  onChoose: (index: number, withModifier: boolean) => void
  /** Close the surface (Escape). */
  onClose: () => void
  /**
   * Alt/Option+Enter handler. Return `true` if the alternate action was
   * handled (so the hook can stop further processing). Optional.
   */
  onAltEnter?: (index: number) => boolean
  /**
   * When the list is empty (`count === 0`), release Enter/Tab instead of
   * swallowing them, and close the surface (default: false).
   *
   * An empty picker has no claim on the keyboard: with nothing to choose,
   * a swallowed Enter silently blocks the host's own Enter action (e.g. the
   * chat composer's Enter-to-send) with no way out but typing a closing
   * character. Surfaces where staying put on an empty list is deliberate
   * (palette-style surfaces that keep focus while the user refines the
   * query) keep the default.
   */
  releaseKeysWhenEmpty?: boolean
}

export interface ListKeyboardNav {
  /** Currently-selected row index. */
  selected: number
  /** Set the selected index (accepts a number or updater). */
  setSelected: Dispatch<SetStateAction<number>>
  /** Live mirror of `selected`, safe to read inside effect-captured callbacks. */
  selectedRef: MutableRefObject<number>
  /** Per-row element refs; assign with `ref={el => { itemRefs.current[i] = el }}`. */
  itemRefs: MutableRefObject<(HTMLElement | null)[]>
  /**
   * The hook's IME-guard claim, for a HOST-LEVEL interceptor that outranks the
   * hook's own document-capture listener (the window-capture Tab takeover the
   * `onChoose` doc describes). Such an interceptor acts on choose-class keys
   * BEFORE the hook's guarded dispatch can decline them, so it must consult
   * the SAME tracked latch: call this first in each intercepted choose branch
   * and bail out on `false` — the claim has already consumed the decline per
   * `claimKey`'s contract in useImeGuard.ts (stopPropagation always,
   * preventDefault only where the browser would otherwise act). A private
   * latch in the host is the drift the `ImeEnterClaimRatchet` pins; this is
   * the sanctioned spelling.
   */
  claimKey: (e: globalThis.KeyboardEvent) => boolean
}

export function useListKeyboardNav(opts: UseListKeyboardNavOptions): ListKeyboardNav {
  const { open, count, onChoose, onClose, onAltEnter, wrap = false, releaseKeysWhenEmpty = false } = opts

  const [selected, setSelected] = useState(0)
  const selectedRef = useRef(0)
  const itemRefs = useRef<(HTMLElement | null)[]>([])

  // Keep latest callbacks/count in refs so the keydown listener can stay
  // attached without re-subscribing on every render.
  const countRef = useRef(count)
  countRef.current = count
  const onChooseRef = useRef(onChoose)
  onChooseRef.current = onChoose
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const onAltEnterRef = useRef(onAltEnter)
  onAltEnterRef.current = onAltEnter

  // IME guard for the hook's keyboard actions. This listener receives NATIVE
  // KeyboardEvents (document capture), which `useImeGuard`'s synthetic-only
  // `claimEnter` cannot consume — so it shares the guard's tracked latch via
  // `createImeLatch` instead of hand-rolling a second spelling. On WebKit the
  // keydown that commits an IME candidate arrives AFTER `compositionend` with
  // `isComposing` already false; unguarded, committing a candidate into the
  // filter query would activate whatever row is highlighted (switch project,
  // insert a file/skill/slash command, dispatch a palette row) against
  // half-composed text. The latch lives in a ref so `onKey` reads the live
  // value without re-subscribing.
  const imeLatchRef = useRef<ReturnType<typeof createImeLatch>>()
  if (!imeLatchRef.current) imeLatchRef.current = createImeLatch()

  const move = useCallback((next: number) => {
    selectedRef.current = next
    setSelected(next)
    // Scroll the freshly-selected row into view if it has been mounted.
    const el = itemRefs.current[next]
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ block: 'nearest' })
    }
  }, [])

  // Reset to the top each time the surface (re)opens.
  useEffect(() => {
    if (!open) return
    selectedRef.current = 0
    setSelected(0)
  }, [open])

  // Clamp the selection if the list shrinks below the current index.
  useEffect(() => {
    if (count > 0 && selectedRef.current >= count) {
      selectedRef.current = count - 1
      setSelected(count - 1)
    }
  }, [count])

  const onKey = useCallback((e: KeyboardEvent) => {
    const n = countRef.current
    if (e.key === 'Escape') {
      // Escape dismisses an IME candidate list too. Let the shared latch own
      // that key while composition is live and in WebKit's short
      // post-composition window, so cancelling a candidate does not also close
      // the picker and discard the user's query.
      if (!imeLatchRef.current!.claimKey(e)) return
      e.preventDefault()
      e.stopPropagation()
      onCloseRef.current()
      return
    }
    if (n === 0) {
      if (e.key === 'Enter' || e.key === 'Tab') {
        if (releaseKeysWhenEmpty) {
          // Nothing to choose: the surface has no claim on the keystroke, so
          // close and let it reach the host (e.g. the composer's
          // Enter-to-send) untouched.
          onCloseRef.current()
          return
        }
        // Nothing to choose: swallow the choose/tab keys so the surface stays put.
        e.preventDefault()
        e.stopPropagation()
      }
      return
    }
    // A choose key the IME owns must not activate the row. `claimKey` owns
    // the whole decline (stopPropagation always; preventDefault only in the
    // post-composition latch window where the browser would otherwise act) —
    // see its contract in useImeGuard.ts. Covers Tab as well as Enter: IMEs
    // use Tab to cycle the candidate list, and Tab is the same unconditional
    // choose dispatch below. Deliberately after the empty-list block above,
    // whose release path must stay untouched (the host composer carries its
    // own IME guard).
    if ((e.key === 'Enter' || e.key === 'Tab') && !imeLatchRef.current!.claimKey(e)) {
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      e.stopPropagation()
      const next = selectedRef.current + 1
      move(wrap ? next % n : Math.min(next, n - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      e.stopPropagation()
      const next = selectedRef.current - 1
      move(wrap ? (next + n) % n : Math.max(next, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      e.stopPropagation()
      if (e.altKey && onAltEnterRef.current) {
        // Honor the documented onAltEnter contract: it returns true when it
        // handled the alternate action; false means fall through to the
        // default choose the pickers rely on.
        if (!onAltEnterRef.current(selectedRef.current)) {
          onChooseRef.current(selectedRef.current, false)
        }
      } else {
        // ⌘/Ctrl held → withModifier=true (modifier branch of the Enter matrix).
        onChooseRef.current(selectedRef.current, e.metaKey || e.ctrlKey)
      }
    } else if (e.key === 'Tab') {
      e.preventDefault()
      e.stopPropagation()
      onChooseRef.current(selectedRef.current, false)
    }
  }, [move, wrap, releaseKeysWhenEmpty])

  // Composition tracking + latch lifecycle, keyed on `open` ONLY. The keydown
  // subscription below re-attaches whenever `onKey`'s identity changes (wrap /
  // releaseKeysWhenEmpty — consumers derive the latter from live query/fetch
  // state), and a latch reset riding along on that churn would clear the
  // post-composition window at exactly the moment it matters: the commit's own
  // input event mutates the query right before the committing keydown arrives.
  // Same constraint useListboxKeyboard states for its guard ("no reset()
  // firing mid-composition on unrelated re-renders").
  useEffect(() => {
    if (!open) return
    const latch = imeLatchRef.current!
    // A reopened surface must not inherit a latch stranded by a composition
    // that ended (or was abandoned) while it was closed.
    latch.reset()
    // Composition tracking listens at document capture like the keydown
    // listener itself: the filter input these surfaces pair with lives in the
    // host (outside the menu), so element-scoped listeners have nothing
    // reliable to attach to.
    const onCompositionStart = () => latch.onCompositionStart()
    const onCompositionEnd = () => latch.onCompositionEnd()
    // Stranded-latch recovery, mirroring bindComposition's blur reset: a
    // composition abandoned WITHOUT compositionend (focus moves off the input
    // mid-composition, an OS-level IME cancel, the element unmounts) would
    // otherwise leave the latch set — and a latched guard consumes what it
    // declines, so the surface would silently stop responding to Enter/Tab.
    const onRecover = () => latch.reset()
    document.addEventListener('compositionstart', onCompositionStart, true)
    document.addEventListener('compositionend', onCompositionEnd, true)
    document.addEventListener('focusout', onRecover, true)
    return () => {
      document.removeEventListener('compositionstart', onCompositionStart, true)
      document.removeEventListener('compositionend', onCompositionEnd, true)
      document.removeEventListener('focusout', onRecover, true)
      // Drop any pending post-composition timer with the listeners so a timer
      // from the closing surface cannot fire into the next open.
      latch.reset()
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [open, onKey])

  // Expose a synced setter that keeps selectedRef in lockstep with the state,
  // so external callers (e.g. resetting to 0 after new results) don't leave the
  // ref stale — which would cause Enter to dispatch on the wrong index.
  const setSelectedSynced: Dispatch<SetStateAction<number>> = useCallback((v) => {
    const next = typeof v === 'function' ? v(selectedRef.current) : v
    move(next)
  }, [move])

  // Stable delegate onto the instance latch, so a host's window-capture
  // interceptor consults the same composition tracking as the hook's own
  // choose dispatch (see the ListKeyboardNav.claimKey contract).
  const claimKey = useCallback(
    (e: globalThis.KeyboardEvent) => imeLatchRef.current!.claimKey(e),
    [],
  )

  return { selected, setSelected: setSelectedSynced, selectedRef, itemRefs, claimKey }
}
