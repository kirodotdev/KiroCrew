import { useLayoutEffect, useSyncExternalStore } from 'react'
import { FOLD_TWO_PANE_QUERY } from '../hooks/useViewportSegments'
import { isChatRoute } from './chatRoute'
import { appPathname } from './basePath'
import { isSidePanelMounted } from '../hooks/sidePanelMounted'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/** The width the phone form ends at; `useIsMobile` re-exports it. Kept here so
 *  the form without a fold (`unfoldedPhoneForm`) reads it from a module that
 *  tests do not stub. */
export const MOBILE_BREAKPOINT = 768

/** Fired on `window` when the route changes (shell/appShell.tsx), so `useIsMobile`
 *  re-reads the form: the fold's two-pane form depends on the route. */
export const LAYOUT_ROUTE_CHANGED_EVENT = 'mc:layout-route-changed'

/** Fire LAYOUT_ROUTE_CHANGED_EVENT when `pathname` changes, from a layout
 *  effect: `useIsMobile` re-reads the form and React re-renders before the new
 *  route paints, so it never paints once in the old route's form. */
export function useLayoutRouteSignal(pathname: string): void {
  useLayoutEffect(() => { window.dispatchEvent(new Event(LAYOUT_ROUTE_CHANGED_EVENT)) }, [pathname])
}

export const TOUCH_LAYOUT_KEY = 'kc-touch-layout'
export const TOUCH_LAYOUT_CHOICES = ['auto', 'phone', 'desktop'] as const
export type TouchLayoutChoice = (typeof TOUCH_LAYOUT_CHOICES)[number]
/** Fired on `window` after the choice changes, so `useIsMobile` re-reads the form at once. */
export const TOUCH_LAYOUT_CHANGED_EVENT = 'mc:touch-layout-changed'

// The choice a write could not save (site storage blocked or full). It wins over
// whatever storage still holds, so the pick applies to this page; the next write
// that lands clears it and storage is the source again.
let unsavedChoice: TouchLayoutChoice | null = null

/** The saved choice; anything missing or unknown reads as Automatic. */
export function readTouchLayoutChoice(): TouchLayoutChoice {
  const stored = safeGetItem(TOUCH_LAYOUT_KEY)
  return unsavedChoice ?? TOUCH_LAYOUT_CHOICES.find(choice => choice === stored) ?? 'auto'
}

export function saveTouchLayoutChoice(choice: TouchLayoutChoice) {
  unsavedChoice = safeSetItem(TOUCH_LAYOUT_KEY, choice) ? null : choice
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(TOUCH_LAYOUT_CHANGED_EVENT))
}

/** Coarse primary pointer that cannot hover: the devices the choice applies to. */
export function isTouchFirst(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(pointer: coarse)').matches && window.matchMedia('(hover: none)').matches
}

/**
 * The layout form a touch-first device asks for: the person's own "Touch screen
 * layout" choice, else the browser's own mobile/desktop mode rather than the
 * window's width.
 *
 * A reported fold at 640px and wider returns `false` (the two-pane form) on the
 * chat route, on any device, ahead of the browser's mode. Only the person's own
 * Phone layout choice comes before it. Every other route keeps the unfolded
 * rule: below 768px only the chat shell takes the two-pane form
 * (rfc-foldable-viewport-segments, "Which poses switch"). A flat foldable reports no fold, so it keeps the rule
 * below.
 *
 * A touch-first device is one whose primary pointer is coarse AND cannot hover.
 * On it the browser already knows which form the person wants: a phone browser
 * reports itself as mobile, its "Request desktop site" toggle (and iPad Safari
 * by default) reports desktop. Width alone got this wrong on a wide phone: an
 * unfolded foldable in landscape is ~950px wide, past the 768px breakpoint, and
 * got the desktop form with none of the phone's touch drawers.
 *
 * A home-screen web app or an in-app browser has no such toggle, so Settings >
 * Display > View offers the same choice (Automatic, Phone layout, Desktop layout
 * on wide screens). It is saved on this browser only and read only here, after
 * the touch-first test, so a computer with a mouse keeps the width breakpoint.
 *
 * Returns `true` (phone form) for the Phone choice, or on Automatic for a
 * mobile-mode browser. Returns `null` (the width breakpoint decides, as before)
 * for everything else, including the Desktop choice and a desktop-mode browser:
 * both get the desktop form from 768px up and keep the phone form below it.
 */
export function touchFirstForm(): boolean | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  if (isTouchFirst() && readTouchLayoutChoice() === 'phone') return true
  if (isChatRoute(appPathname()) && window.matchMedia(FOLD_TWO_PANE_QUERY).matches) return false
  return touchFirstUnfoldedForm()
}

/** The phone/desktop form with any fold left out: `useIsMobile`'s rule without
 *  FOLD_TWO_PANE_QUERY, the touch-first answer, else the width breakpoint. The
 *  side panel picks its host from this (`panelInlineHost` below),
 *  so a fold, which changes the form, never changes where the panel is mounted. */
export function unfoldedPhoneForm(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return touchFirstUnfoldedForm() ?? window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`).matches
}

// The unfolded form as of `useIsMobile`'s last commit, or as refreshed since
// by `refreshUnfoldedForm` (a no-op wake with no panel, or the last panel
// unmounting). Kept here, not in hooks/useIsMobile.ts, because many suites
// stub that module whole.
let committedUnfolded: boolean | null = null

const unfoldedListeners = new Set<() => void>()

/** Subscribe to changes of the committed unfolded form that no form switch
 *  carries (`refreshUnfoldedForm`), for `useSyncExternalStore`: the chat page
 *  reads the panel's host through it, so the host moves in the commit the
 *  change causes, not at some later render. */
export function subscribeUnfoldedForm(cb: () => void): () => void {
  unfoldedListeners.add(cb)
  return () => { unfoldedListeners.delete(cb) }
}

/** Record the unfolded form afresh and notify subscribers when it changed.
 *  The one path for a change no form switch carries: a no-op wake with no
 *  panel mounted (`recordUnfoldedForm`), and the last panel unmounting
 *  (hooks/sidePanelMounted.ts). A no-op while nothing tracks the form, or in a
 *  window with no viewport. */
export function refreshUnfoldedForm(): void {
  if (committedUnfolded === null) return
  if (typeof window !== 'undefined' && window.innerWidth === 0 && window.innerHeight === 0) return
  const next = unfoldedPhoneForm()
  if (next === committedUnfolded) return
  committedUnfolded = next
  for (const cb of [...unfoldedListeners]) cb()
}

/** Record the unfolded form at a `useIsMobile` commit; `null` when it stops
 *  tracking. A window with no viewport keeps the form it last recorded.
 *  Invariant: the panel's host holds only for a mounted panel's continuity. A
 *  wake that switched no form (`formChanged` false: a resize across 768px that
 *  a fold keeps two-pane, or laying the device flat where the form stays
 *  desktop) keeps the recorded form while a side panel is mounted
 *  (hooks/sidePanelMounted.ts), since moving its host there would remount it
 *  and reload an open app frame outside any morph. With no panel mounted there
 *  is nothing to reload, so the wake refreshes the form (`refreshUnfoldedForm`),
 *  as does the held panel unmounting. With no fold the form already follows
 *  the unfolded rule, so this changes nothing there.
 *  The held state's cost: a held panel stays inline in the desktop form (under
 *  the top bar, no bottom dock, side-panel health heal off) until it unmounts. */
export function recordUnfoldedForm(tracking: boolean, formChanged = true): void {
  if (!tracking) { committedUnfolded = null; return }
  if (typeof window !== 'undefined' && window.innerWidth === 0 && window.innerHeight === 0) return
  if (!formChanged && committedUnfolded !== null) {
    if (!isSidePanelMounted()) refreshUnfoldedForm()
    return
  }
  committedUnfolded = unfoldedPhoneForm()
}

/** The unfolded form `useIsMobile` last committed, so a render between a
 *  viewport crossing 768px and the form switch's commit reads the old form, as
 *  `useIsMobile` does. Where nothing has committed one it reads the form live,
 *  and a window with no viewport (0x0) reads as `useIsMobile` reads it there:
 *  the touch-first answer, else the desktop form, never the narrow query that a
 *  0x0 window matches. */
export function committedUnfoldedPhoneForm(): boolean {
  if (committedUnfolded !== null) return committedUnfolded
  if (typeof window !== 'undefined' && window.innerWidth === 0 && window.innerHeight === 0) {
    return touchFirstUnfoldedForm() ?? false
  }
  return unfoldedPhoneForm()
}

/** `touchFirstForm` as it reads with no fold reported: the touch-first rule alone. */
export function touchFirstUnfoldedForm(): true | null {
  if (!isTouchFirst()) return null
  const choice = readTouchLayoutChoice()
  if (choice !== 'auto') return choice === 'phone' ? true : null
  // Mobile mode promotes to the phone form at any width. Desktop mode leaves
  // the width to decide: below 768px (iPad Split View or Slide Over, a tablet
  // in multi-window) the desktop top bar's collapse ladder has no rungs, so
  // those windows keep the phone form they had.
  return browserReportsMobile() ? true : null
}

/** The browser's own mobile/desktop mode: User-Agent Client Hints where the
 *  browser has them, else the `Mobile` token every phone browser's UA string
 *  carries (and its desktop-site mode drops). */
export function browserReportsMobile(): boolean {
  if (typeof navigator === 'undefined') return false
  const hinted = (navigator as Navigator & { userAgentData?: { mobile?: unknown } }).userAgentData?.mobile
  if (typeof hinted === 'boolean') return hinted
  return /\bMobile\b/.test(navigator.userAgent || '')
}

/**
 * Whether the side panel is mounted inline, beside the chat in the page's own
 * row, rather than portaled into the shell's full-height column. The phone
 * form needs the inline host. Off it the choice follows the form the device
 * would have WITHOUT a fold, so folding or unfolding, which flips the form at
 * the same width, never moves the panel to the other host. Moving it would
 * mount a second panel and drop every live frame in it (an app frame's form, a
 * preview's page). The unfolded form is the one `useIsMobile` last committed,
 * or refreshed since (`refreshUnfoldedForm`, also run when the last panel
 * unmounts, so with no panel mounted it can lead `isMobile` by a frame)
 * (`committedUnfoldedPhoneForm`), not the live breakpoint: with no fold the
 * host changes in the same commit as the form, inside the form switch's morph,
 * so a resize across 768px moves the panel as on main and never a render early.
 *
 * A window resized across 768px while folded, a Touch screen layout choice
 * changed while folded, or laying the device flat where the form stays
 * desktop, switches no form. There the host is held while a panel is mounted
 * (`recordUnfoldedForm`): moving it would remount the panel outside any morph
 * and reload its frames. With no panel mounted, or once the held panel
 * unmounts, the host follows the form (`refreshUnfoldedForm`).
 */
export function panelInlineHost({ isMobile }: { isMobile: boolean }): boolean {
  if (isMobile) return true
  if (typeof window === 'undefined') return false
  // A window with no viewport (a background tab, or a remote dashboard pane
  // hidden with display:none) has no form to read. useIsMobile keeps the
  // committed form there, and the host reads the same committed form, so hiding
  // a folded pane never moves the panel or reloads its frames.
  return committedUnfoldedPhoneForm()
}

/** `panelInlineHost` for a component: subscribed to `refreshUnfoldedForm`, so
 *  a change no form switch carries moves the host in the commit it causes. */
export function usePanelInlineHost({ isMobile }: { isMobile: boolean }): boolean {
  const read = () => panelInlineHost({ isMobile })
  return useSyncExternalStore(subscribeUnfoldedForm, read, read)
}
