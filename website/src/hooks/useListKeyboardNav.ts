import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, MutableRefObject, SetStateAction } from 'react'

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
 *  - `Alt`/`Option`+`Enter`  — `onAltEnter(selected)` when provided; if it
 *    returns `true` the event is treated as handled.
 *  - `Escape`                — `onClose()`.
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
}

export function useListKeyboardNav(opts: UseListKeyboardNavOptions): ListKeyboardNav {
  const { open, count, onChoose, onClose, onAltEnter, wrap = false } = opts

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
      e.preventDefault()
      e.stopPropagation()
      onCloseRef.current()
      return
    }
    if (n === 0) {
      // Nothing to choose: swallow the choose/tab keys so the surface stays put.
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        e.stopPropagation()
      }
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
        // default choose (the earlier behavior the pickers rely on).
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
  }, [move, wrap])

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

  return { selected, setSelected: setSelectedSynced, selectedRef, itemRefs }
}
