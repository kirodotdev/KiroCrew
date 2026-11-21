import { FOLD_TWO_PANE_QUERY } from '../hooks/useViewportSegments'

/** The width the phone form ends at; `useIsMobile` re-exports it. Kept here so
 *  the form without a fold (`unfoldedPhoneForm`) reads it from a module that
 *  tests do not stub. */
export const MOBILE_BREAKPOINT = 768

/**
 * The layout form a touch-first device asks for, read from the browser's own
 * mobile/desktop mode rather than the window's width.
 *
 * One input comes first, on any device: a reported fold at 640px and wider
 * returns `false` (the two-pane form), ahead of the touch-first phone rule.
 * A flat foldable reports no fold, so it keeps the rule below.
 *
 * A touch-first device is one whose primary pointer is coarse AND cannot hover.
 * On it the browser already knows which form the person wants: a phone browser
 * reports itself as mobile, its "Request desktop site" toggle (and iPad Safari
 * by default) reports desktop. Width alone got this wrong on a wide phone: an
 * unfolded foldable in landscape is ~950px wide, past the 768px breakpoint, and
 * got the desktop form with none of the phone's touch drawers.
 *
 * Returns `true` (phone form) for a mobile-mode browser on a touch-first device,
 * or `null` (the width breakpoint decides, as before) for everything else,
 * including a desktop-mode browser: it gets the desktop form from 768px up.
 *
 * shortcut: a home-screen web app has no "Request desktop site" toggle, so it
 * keeps the browser's default mode; revisit if that needs a setting of its own.
 */
export function touchFirstForm(): boolean | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  if (window.matchMedia(FOLD_TWO_PANE_QUERY).matches) return false
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

// The unfolded form as of `useIsMobile`'s last commit. Kept here, not in
// hooks/useIsMobile.ts, because many suites stub that module whole.
let committedUnfolded: boolean | null = null

/** Record the unfolded form at a `useIsMobile` commit; `null` when it stops
 *  tracking. A window with no viewport keeps the form it last recorded. */
export function recordUnfoldedForm(tracking: boolean): void {
  if (!tracking) { committedUnfolded = null; return }
  if (typeof window !== 'undefined' && window.innerWidth === 0 && window.innerHeight === 0) return
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
export function touchFirstUnfoldedForm(): boolean | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  const touchFirst = window.matchMedia('(pointer: coarse)').matches && window.matchMedia('(hover: none)').matches
  if (!touchFirst) return null
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
 * preview's page). The unfolded form is the one `useIsMobile` last committed
 * (`committedUnfoldedPhoneForm`), not the live breakpoint: with no fold the
 * host changes in the same commit as the form, inside the form switch's morph,
 * so a resize across 768px moves the panel as on main and never a render early.
 *
 * shortcut: a window resized across 768px while folded queues no form switch,
 * so the host moves at the next render outside any morph; it remounts the
 * panel there, as main does on any resize across 768px.
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
