/**
 * The "Reduce glass transparency" display setting.
 *
 * A browser-local preference, like the font family and the theme mode: it is
 * about how THIS screen renders, not about the gateway. When it is on, the
 * root element carries `data-reduce-transparency="on"` and index.css applies
 * the same solidifying rules it applies under the OS-level
 * `prefers-reduced-transparency: reduce` media query -- every Liquid Glass
 * pane becomes an opaque `--bg-elevated` card with its effect layers hidden,
 * and a focused pane takes the app's standard outline. One rule set, two
 * triggers (the OS setting and this switch), see the mirror block in index.css.
 *
 * `index.html` reads the same key before React hydrates so the first paint
 * already renders solid; this module is the single owner of the key name and
 * the attribute value so the two cannot drift.
 */
export const REDUCE_TRANSPARENCY_STORAGE_KEY = 'mc-reduce-transparency'

export function readReduceTransparency(): boolean {
  try {
    return localStorage.getItem(REDUCE_TRANSPARENCY_STORAGE_KEY) === 'on'
  } catch {
    return false
  }
}

export function applyReduceTransparency(on: boolean): void {
  if (typeof document === 'undefined') return
  document.documentElement.dataset.reduceTransparency = on ? 'on' : 'off'
}

export function persistReduceTransparency(on: boolean): void {
  try {
    if (on) localStorage.setItem(REDUCE_TRANSPARENCY_STORAGE_KEY, 'on')
    else localStorage.removeItem(REDUCE_TRANSPARENCY_STORAGE_KEY)
  } catch {
    // Storage may be unavailable (private mode, quota); the attribute still applies for this session.
  }
}
