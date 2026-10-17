import { useSyncExternalStore } from 'react'

/**
 * Where theme-pack decoration paints relative to the dashboard chrome (#7377).
 *
 * The dashboard shell (`data-testid="dashboard-shell"` in App.tsx) is
 * `relative z-[1]`, so it is its own stacking context: every z-index INSIDE it —
 * the top bar's included — is ordered only against its siblings in the shell,
 * and the shell as a whole sits at z=1 against the root. A `position: fixed`
 * overlay rendered as a sibling of `<App />` therefore competes with the shell's
 * z=1, never with the top bar's z=45, and wins with any z-index above 1 — even
 * an overlay clamped to 44 paints over the header. The only way an overlay can
 * sit above the chat surface but below the chrome is to be a child of the same
 * stacking context as the chrome: the shell.
 *
 * So the shell renders one slot, `ThemeExperienceLayer` portals its decorative
 * overlays into it, and the slot itself is a fixed, click-through stacking
 * context pinned at `OVERLAY_Z_MAX`. Being a stacking context is what makes the
 * ceiling structural: nothing rendered inside can rise above the slot's own
 * z-index however large a value the manifest asks for.
 */

/** DOM id of the in-shell slot theme overlays portal into. */
export const THEME_DECOR_SLOT_ID = 'theme-decor-slot'

/** The top bar's z-index while it is a grid row of the shell (App.tsx header). */
export const TOPBAR_Z = 45

/**
 * The top bar's z-index in focus mode, where it leaves the grid and becomes an
 * absolute overlay clearing the chat-pane stack (max 61) and the rail (50).
 */
export const TOPBAR_FOCUS_Z = 62

/**
 * Ceiling for a pack overlay's zIndex, and the slot's own z-index. Derived, not
 * chosen: strictly below the top bar in BOTH its layouts so the ordering cannot
 * drift back into a tie (the pre-#7377 state was 45 vs 45, decided by DOM order).
 */
export const OVERLAY_Z_MAX = Math.min(TOPBAR_Z, TOPBAR_FOCUS_Z) - 1

// `ThemeExperienceLayer` mounts ABOVE the router (main.tsx) and outlives every
// shell — onboarding, bootstrap and the multi-instance viewport all render
// without one — so the slot cannot be passed down as a prop. The shell registers
// it from a ref callback (null again on unmount) and the layer subscribes; a
// module-level store, same shape as `useFocusMode`, so there is exactly one
// slot per document and no DOM polling.
let slot: HTMLElement | null = null
const listeners = new Set<() => void>()

/** Ref callback for the shell's slot element. */
export function registerThemeDecorSlot(el: HTMLElement | null): void {
  if (slot === el) return
  slot = el
  listeners.forEach(l => l())
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

const getSnapshot = () => slot

/** The in-shell slot, or null while no shell is mounted (render inline then). */
export function useThemeDecorSlot(): HTMLElement | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** Test seam: restore the module default between cases. */
export function __resetThemeDecorSlot(): void {
  slot = null
  listeners.clear()
}
