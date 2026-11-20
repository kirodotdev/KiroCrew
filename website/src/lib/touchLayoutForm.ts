import { safeGetItem, safeSetItem } from '../utils/safeStorage'

export const TOUCH_LAYOUT_KEY = 'kc-touch-layout'
export const TOUCH_LAYOUT_CHOICES = ['auto', 'phone', 'desktop'] as const
export type TouchLayoutChoice = (typeof TOUCH_LAYOUT_CHOICES)[number]
/** Fired on `window` after the choice changes, so `useIsMobile` re-reads the form at once. */
export const TOUCH_LAYOUT_CHANGED_EVENT = 'mc:touch-layout-changed'

/** The saved choice; anything missing or unknown reads as Automatic. */
export function readTouchLayoutChoice(): TouchLayoutChoice {
  const stored = safeGetItem(TOUCH_LAYOUT_KEY)
  return TOUCH_LAYOUT_CHOICES.find(choice => choice === stored) ?? 'auto'
}

// shortcut: with site storage blocked the choice is not kept and reads as
// Automatic again; revisit if that shows up in practice.
export function saveTouchLayoutChoice(choice: TouchLayoutChoice) {
  safeSetItem(TOUCH_LAYOUT_KEY, choice)
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
export function touchFirstForm(): true | null {
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
