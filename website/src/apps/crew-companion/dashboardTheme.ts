/**
 * Give a standalone app window the dashboard's live theme.
 *
 * The problem: an app window is its own page entry, and the bundler emits no
 * stylesheet link for it — so none of Kiro Crew's CSS variables exist there. The
 * panel is styled entirely from those variables, so without this it silently falls
 * back to the hardcoded literals in `panelSkin` and ignores the user's theme.
 *
 * Why not a fixed palette: the sibling companion app ships one (`applyFallbackTheme`),
 * which is honest about being a fallback but cannot follow a theme change or serve
 * the ~36 themes Kiro Crew offers. The panel already resolves every colour through a
 * variable, so the only missing piece is the variables themselves.
 *
 * Why discover the hrefs at runtime rather than hardcode them: the dashboard's
 * stylesheets are content-hashed AND code-split, so any literal path goes stale on
 * the next build and fails silently — a theme that quietly stops working is worse
 * than one that never did. The window is same-origin with the dashboard, so it can
 * read the dashboard's own document and adopt whatever stylesheets it currently
 * links.
 *
 * Why adopt EVERY linked stylesheet, not one guessed by name: the CSS variables and
 * the `[data-theme=…]` blocks live in whichever split chunk Vite happens to emit
 * them in (today `src-*.css`; the entry chunk is `main-*.css` and carries almost
 * none of them). A name heuristic that targets one chunk (`index-*.css`) silently
 * picked the wrong file after a chunk rename and left every variable undefined —
 * which is exactly the fallback-colour bug this module exists to prevent. Injecting
 * all of them is naming-agnostic and future-proof; the transparency guard below
 * absorbs the body-painting rules that come with them.
 */

// The rules themselves live in a stylesheet (see that file's header for why);
// imported as text because this guard must be injected LAST into <head>.
import transparentCss from './windowTransparent.css?raw'

/** Marks each stylesheet link we inject so a second call is a no-op. */
const THEME_LINK_ATTR = 'data-cc-dashboard-theme'
/** Id of the style element that guarantees window transparency. */
const TRANSPARENT_STYLE_ID = 'cc-window-transparent'

/**
 * Re-assert transparency AFTER the stylesheet lands.
 *
 * The dashboard stylesheet paints `body` (`background: var(--bg)`) AND a fixed,
 * full-viewport animated `body::before` gradient — so adopting it naively turns the
 * overlay into an opaque (or faintly tinted, animating) rectangle over everything.
 *
 * Two layers, because the two targets need different mechanisms:
 *   1. `documentElement`/`body` background — an element's own inline declaration
 *      outranks any stylesheet rule, even an `!important` one, so an inline
 *      `transparent !important` cannot lose a specificity race with the dashboard's
 *      body rules however they are ordered.
 *   2. `body::before` / `body::after` — a pseudo-element cannot be reached by an
 *      inline style, so it is neutralized by a dedicated rule in a `<style>` element
 *      kept as the LAST child of <head>. `!important` beats the dashboard's
 *      (non-important) gradient rules regardless of their higher selector
 *      specificity, and being last wins any `!important` tie by source order.
 */
function keepWindowTransparent(): void {
  for (const el of [document.documentElement, document.body]) {
    if (!el) continue
    el.style.setProperty('background', 'transparent', 'important')
    el.style.setProperty('background-image', 'none', 'important')
  }

  let guard = document.getElementById(TRANSPARENT_STYLE_ID) as HTMLStyleElement | null
  if (!guard) {
    guard = document.createElement('style')
    guard.id = TRANSPARENT_STYLE_ID
  }
  guard.textContent = transparentCss
  // (Re)appending an existing node moves it to the end of <head>, so the guard
  // always sits after any stylesheet link injected above it.
  document.head.appendChild(guard)
}

/**
 * Extract every stylesheet href a document links, in order, de-duplicated.
 *
 * Exported so the discovery logic can be pinned by a test without a live fetch or a
 * real stylesheet load. Matches `<link … href="….css">` regardless of attribute
 * order (the built HTML emits `rel`/`crossorigin` before `href`).
 */
export function extractStylesheetHrefs(html: string): string[] {
  const hrefs = [...html.matchAll(/<link\b[^>]*\bhref="([^"]+\.css)"[^>]*>/gi)].map((m) => m[1])
  return [...new Set(hrefs)]
}

/**
 * Find the dashboard's current stylesheet URLs by reading its own document.
 *
 * @returns every linked stylesheet href, or an empty array when none can be found
 */
async function findDashboardStylesheets(): Promise<string[]> {
  try {
    const res = await fetch('/', { credentials: 'same-origin' })
    if (!res.ok) return []
    return extractStylesheetHrefs(await res.text())
  } catch {
    return []
  }
}

/**
 * Adopt the dashboard's theme in this window: its variables, and its active theme id.
 *
 * Resolves once the stylesheets have loaded (or immediately if none can be found), so
 * a caller can render after the variables exist and avoid a flash of fallback colour.
 */
export async function adoptDashboardTheme(): Promise<void> {
  applyThemeId()
  keepWindowTransparent()

  // Idempotent: a second call must not inject a second copy of every sheet.
  if (document.querySelector(`link[${THEME_LINK_ATTR}]`)) {
    keepWindowTransparent()
    return
  }

  const hrefs = await findDashboardStylesheets()
  if (hrefs.length === 0) {
    keepWindowTransparent() // the panel's own fallbacks stand in
    return
  }

  await Promise.all(
    hrefs.map(
      (href) =>
        new Promise<void>((resolve) => {
          const link = document.createElement('link')
          link.rel = 'stylesheet'
          link.href = href
          link.setAttribute(THEME_LINK_ATTR, '')
          // Resolve either way: a missing stylesheet must not stop the companion appearing.
          link.onload = () => resolve()
          link.onerror = () => resolve()
          document.head.appendChild(link)
        }),
    ),
  )

  // Last word on transparency, after the dashboard's body rules are in play.
  keepWindowTransparent()
}

/**
 * Set `data-theme` (and `data-mode`), which is what the stylesheet keys its variable
 * blocks on — without it, every `var(--…)` resolves to the `:root` LIGHT defaults, so
 * a dark-theme user gets a light panel.
 *
 * Read from the same localStorage keys the dashboard writes (`mc-color-theme` /
 * `mc-theme`), which works because the window is same-origin. This is deliberately
 * NOT read from the fetched `/` document: the served `index.html` hardcodes
 * `<html data-theme="dark">` as a pre-hydration placeholder and never reflects the
 * user's real choice — that is set at runtime by the dashboard's React tree, which
 * this window cannot observe. localStorage is the one shared source of truth.
 *
 * The theme-id shape mirrors the dashboard's own `applyTheme`: `emerald` is the
 * unprefixed default (`dark`/`light`); a custom theme's value is already
 * `custom-<slug>`, so the generic `${color}-${mode}` branch yields the matching
 * `custom-<slug>-<mode>`. Split out so it can also run on a `storage` event, when the
 * user changes theme while the companion is open.
 */
export function applyThemeId(): void {
  try {
    const color = localStorage.getItem('mc-color-theme') || 'kiro'
    const pref = localStorage.getItem('mc-theme') || 'system'
    // Mirror the dashboard's getSystemMode(): dark when the OS prefers dark, else light.
    const mode = pref === 'system'
      ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
      : pref
    const el = document.documentElement
    el.dataset.theme = color === 'emerald' ? mode : `${color}-${mode}`
    el.dataset.mode = mode
  } catch {
    // A blocked localStorage just means the default theme.
  }
}

/** Follow the dashboard when the user switches theme while the companion is open. */
export function watchThemeChanges(): () => void {
  const onStorage = (e: StorageEvent) => {
    if (e.key === 'mc-color-theme' || e.key === 'mc-theme') applyThemeId()
  }
  window.addEventListener('storage', onStorage)
  return () => window.removeEventListener('storage', onStorage)
}
