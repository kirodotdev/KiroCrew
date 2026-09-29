/**
 * The "Translucent panels" display setting (Settings -> Display -> View) --
 * opt-in. It turns the Liquid Glass primitive on; the user-facing name is not
 * the primitive's.
 *
 * A browser-local preference, like the font family and the theme mode: it is
 * about how THIS screen renders, not about the gateway. Off (the default) the
 * root element carries `data-reduce-transparency="on"` and index.css applies
 * the same solidifying rules it applies under the OS-level
 * `prefers-reduced-transparency: reduce` media query -- every Liquid Glass
 * pane is an opaque `--bg-elevated` card with its effect layers hidden. On,
 * the attribute is `off` and the panes render as frosted glass over whatever
 * scrolls under them. One rule set, two triggers (the OS setting and this
 * switch), see the mirror block in index.css.
 *
 * The stored key names the thing the user turned ON (`mc-liquid-glass`), so an
 * absent key -- a fresh browser, a cleared store, a blocked store -- is the
 * solid default and never the effect. `index.html` reads the same key before
 * React hydrates so the first paint already matches; this module is the single
 * owner of the key name and the attribute value so the two cannot drift.
 */
export const LIQUID_GLASS_STORAGE_KEY = 'mc-liquid-glass'

export function readLiquidGlass(): boolean {
  try {
    return localStorage.getItem(LIQUID_GLASS_STORAGE_KEY) === 'on'
  } catch {
    return false
  }
}

/** Glass on -> the solidifying attribute is `off`; glass off -> it is `on`. */
export function applyLiquidGlass(on: boolean): void {
  if (typeof document === 'undefined') return
  document.documentElement.dataset.reduceTransparency = on ? 'off' : 'on'
}

export function persistLiquidGlass(on: boolean): void {
  try {
    if (on) localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    else localStorage.removeItem(LIQUID_GLASS_STORAGE_KEY)
  } catch {
    // Storage may be unavailable (private mode, quota); the attribute still applies for this session.
  }
}
