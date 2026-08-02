import { useState, useEffect, useCallback, useRef } from 'react'
import { SHORTCUTS_ENABLED_KEY } from './useKeyboardShortcuts'

/**
 * Global trigger + open/close state for the Search Everywhere command palette
 *. Owns two bindings:
 *
 *  - **double-Shift** (primary): two bare `Shift` keydowns within
 *    {@link DOUBLE_SHIFT_WINDOW_MS}. This is the IntelliJ "Search Everywhere"
 *    gesture and is page-safe — a lone Shift has no default browser action, so
 *    nothing needs cancelling and it never collides with a real shortcut.
 *  - **⌘K / Ctrl+K** (alias): toggles the palette. `preventDefault()` keeps a
 *    browser/extension binding (e.g. some "focus search") from stealing it.
 *    ⌘K is intentionally chosen over reserved combos (⌘P print, ⌘W close tab,
 *    ⌘1–9 tab switch, ⌘⌥← / ⌘⌥→) which Chrome will NOT let a page cancel.
 *
 * Both keyboard bindings honour the global "Enable shortcuts" toggle
 * ({@link SHORTCUTS_ENABLED_KEY}, shared with useKeyboardShortcuts /
 * useInstanceShortcuts and surfaced by the Settings → Shortcuts "Search
 * Everywhere" row). The flag is read live per keystroke — a Settings change
 * takes effect without re-registering the listener — so when shortcuts are
 * off, double-Shift and ⌘K fall through to the browser. The palette stays
 * reachable via its nav button, which calls `openPalette` directly and is
 * unaffected by the gate.
 *
 * The listener is attached to `window` (not an element) so the gesture works
 * regardless of focus — including while a chat textarea or a nav row is
 * focused — matching the "search everywhere" intent.
 *
 * Mount the returned `open`/`close` into a single `<CommandPalette/>` at the
 * app shell; the palette itself owns the in-modal keyboard nav (arrows / Enter
 * / Tab / Esc) via `useListKeyboardNav`, so this hook deliberately stops at the
 * trigger + visibility toggle.
 */

/**
 * Max gap between the two Shift keydowns to count as a double-tap. Kept
 * deliberately tight (250ms): a wider window makes the palette pop open on
 * incidental Shift taps (e.g. reaching for a capital, then hesitating) that
 * were never meant as the gesture. 250ms is comfortably reachable as an
 * intentional double-tap while cutting most accidental opens; ⌘K / Ctrl+K and
 * the nav button remain as unambiguous alternatives.
 */
export const DOUBLE_SHIFT_WINDOW_MS = 250

/** Live read of the global shortcuts toggle (default on; '0' means disabled). */
const shortcutsEnabled = () => localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0'

export interface UseCommandPalette {
  /** Whether the palette is currently shown. */
  open: boolean
  /** Force the palette open (e.g. from a nav button). */
  openPalette: () => void
  /** Close the palette (Escape / overlay click / post-activation). */
  close: () => void
  /** Flip open ⇄ closed (backs the ⌘K alias). */
  toggle: () => void
}

export function useCommandPalette(): UseCommandPalette {
  const [open, setOpen] = useState(false)
  // Timestamp of the last *bare* Shift keydown, for double-tap detection.
  // 0 means "no pending first tap".
  const lastShiftRef = useRef(0)

  const openPalette = useCallback(() => setOpen(true), [])
  const close = useCallback(() => setOpen(false), [])
  const toggle = useCallback(() => setOpen(v => !v), [])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // Honour the global "Enable shortcuts" toggle. Read live so a Settings
      // change applies without re-registering. When off, both bindings fall
      // through to the browser; the palette's nav button (openPalette) is
      // unaffected. Reset any pending first Shift tap so re-enabling can't
      // resume a half-formed double-tap from across the disabled window.
      if (!shortcutsEnabled()) {
        lastShiftRef.current = 0
        return
      }

      // ⌘K / Ctrl+K alias — toggle. Require no other modifiers so it doesn't
      // swallow richer combos. Cancelable in a real Chrome tab.
      if ((e.key === 'k' || e.key === 'K') && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
        e.preventDefault()
        lastShiftRef.current = 0
        setOpen(v => !v)
        return
      }

      // double-Shift (primary) — open. Two bare Shift presses in quick
      // succession. Modifier-combined Shift (Shift+Enter, Shift+letter for
      // capitals, Alt+Shift+… app shortcuts) is excluded via the modifier
      // guard, and held-Shift auto-repeat is ignored.
      if (e.key === 'Shift' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        if (e.repeat) return
        const now = Date.now()
        if (lastShiftRef.current !== 0 && now - lastShiftRef.current <= DOUBLE_SHIFT_WINDOW_MS) {
          lastShiftRef.current = 0
          setOpen(true)
        } else {
          lastShiftRef.current = now
        }
        return
      }

      // Any non-Shift key between the two taps cancels the pending first tap,
      // so "Shift, type something, Shift" never spuriously opens the palette.
      lastShiftRef.current = 0
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  return { open, openPalette, close, toggle }
}
