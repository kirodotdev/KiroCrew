/**
 * The layout form a touch-first device asks for, read from the browser's own
 * mobile/desktop mode rather than the window's width.
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
export function touchFirstForm(): true | null {
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
