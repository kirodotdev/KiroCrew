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
 *
 * The browser copy is the render cache. The choice itself is workspace-backed
 * like the theme mode (`dashboard.translucent_panels`, read at boot through
 * `GET /api/theme/boot`, written through `PUT /api/config/theme`), because a
 * browser-only store is per ORIGIN: a new gateway port, `localhost` against
 * `127.0.0.1`, a second browser or the desktop app each start from an absent
 * key, which read as "the glass keeps turning itself off". `useLiquidGlass`
 * publishes every change; `useTheme`'s boot effect adopts the stored value
 * (`adoptServerLiquidGlass`).
 */
export const LIQUID_GLASS_STORAGE_KEY = 'mc-liquid-glass'

/**
 * Fired on `window` whenever this browser's stored choice changes -- by the
 * switch (any mounted `useLiquidGlass`) or by the boot adoption of the
 * workspace value -- so every mounted hook re-reads the store and the Settings
 * row, the first-run row and the root attribute never disagree.
 */
export const LIQUID_GLASS_CHANGED_EVENT = 'mc-liquid-glass-changed'

/**
 * The choice as the workspace stores it (`dashboard.translucent_panels`;
 * `TRANSLUCENT_PANELS_VALUES` on the gateway). `''` is "never chosen on this
 * workspace": a bool could not tell a workspace that chose solid from one that
 * predates the key, and on the latter the browser's own stored choice must keep
 * holding, or every upgrade would turn an existing user's glass off.
 */
export type TranslucentPanelsStored = '' | 'on' | 'off'

export function toTranslucentPanelsStored(on: boolean): TranslucentPanelsStored {
  return on ? 'on' : 'off'
}

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

/**
 * The new value rides in the event (`detail`) rather than being re-read from
 * the store by each listener: with site storage blocked, `persistLiquidGlass`
 * cannot write, and a listener that re-read would answer the solid default
 * over the value the user just chose, turning the switch straight back off.
 */
export function notifyLiquidGlassChanged(on: boolean): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent<boolean>(LIQUID_GLASS_CHANGED_EVENT, { detail: on }))
}

/** The value a `LIQUID_GLASS_CHANGED_EVENT` carries; the store only when a
 *  foreign dispatch carried none. */
export function liquidGlassFromEvent(event: Event): boolean {
  const detail = (event as CustomEvent<unknown>).detail
  return typeof detail === 'boolean' ? detail : readLiquidGlass()
}

/**
 * Apply the workspace's stored choice to this browser at boot. `'on'` / `'off'`
 * win over the browser copy (the workspace is the source of truth, the browser
 * the cache, as for `mc-theme`); `''` or anything else leaves the browser copy
 * alone, so a workspace that predates the key renders exactly as it did.
 * Returns true when the browser copy changed (the hooks are then notified).
 */
export function adoptServerLiquidGlass(stored: unknown): boolean {
  if (stored !== 'on' && stored !== 'off') return false
  // The workspace holds a choice, so the first-run default has nothing left to
  // offer in this browser: without the marker, a tour left unfinished after
  // the user turned the glass off would seed it on again and publish that
  // over the workspace's 'off'. Before the unchanged-value return on purpose.
  markLiquidGlassSeeded()
  const on = stored === 'on'
  if (readLiquidGlass() === on) return false
  persistLiquidGlass(on)
  applyLiquidGlass(on)
  notifyLiquidGlassChanged(on)
  return true
}

/**
 * Written once, by the first-run "Pick your look" step, the first time it seeds
 * the switch for a fresh install (`components/OnboardingFlow.tsx`). Off is
 * stored as the ABSENT `mc-liquid-glass`, so without this marker a user who
 * turned the glass off and then reloaded before finishing the tour (or came
 * Back from step 2) would be read as a fresh install and seeded on again. The
 * marker, not the key, is what says "the default has been offered here".
 */
export const LIQUID_GLASS_SEEDED_KEY = 'mc-liquid-glass-seeded'

/** True when the first-run default was already offered in this browser (or
 *  the store cannot be read, in which case nothing could persist anyway). */
export function liquidGlassSeeded(): boolean {
  try {
    return localStorage.getItem(LIQUID_GLASS_SEEDED_KEY) !== null
  } catch {
    return true
  }
}

export function markLiquidGlassSeeded(): void {
  try {
    localStorage.setItem(LIQUID_GLASS_SEEDED_KEY, '1')
  } catch {
    // Blocked store: the glass setting itself cannot persist either.
  }
}
