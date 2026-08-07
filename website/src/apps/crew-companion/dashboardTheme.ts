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
 * Why discover the href at runtime rather than hardcode it: the dashboard's
 * stylesheet is content-hashed, so any literal path goes stale on the next build and
 * fails silently — a theme that quietly stops working is worse than one that never
 * did. The window is same-origin with the dashboard, so it can read the dashboard's
 * own document and use whatever stylesheet that is currently linking.
 */

/** Marks our injected link so a second call is a no-op. */
const LINK_ID = 'cc-dashboard-theme'

/**
 * Re-assert transparency AFTER the stylesheet lands.
 *
 * The dashboard stylesheet paints `body`, and this window is transparent and covers
 * the whole display — so adopting it naively turns the overlay into an opaque
 * rectangle over everything. The page's own rules say `transparent !important`, but
 * a later stylesheet with equal specificity and its own `!important` can still win,
 * so the guarantee is re-stated last, in a style element appended after the link.
 */
function keepWindowTransparent(): void {
  // Inline styles, not an injected stylesheet: an element's own style declaration
  // outranks any sheet, so this cannot lose a specificity race with the dashboard's
  // body rules however they are ordered.
  for (const el of [document.documentElement, document.body]) {
    if (!el) continue
    el.style.setProperty('background', 'transparent', 'important')
    el.style.setProperty('background-image', 'none', 'important')
  }
}

/**
 * Find the dashboard's current stylesheet URL by reading its own document.
 *
 * @returns the href, or null when it cannot be determined
 */
async function findDashboardStylesheet(): Promise<string | null> {
  try {
    const res = await fetch('/', { credentials: 'same-origin' })
    if (!res.ok) return null
    const html = await res.text()
    // The dashboard links its bundled CSS from <head>; take the first entry that
    // looks like the app's own stylesheet rather than a vendored one.
    const hrefs = [...html.matchAll(/<link[^>]+href="([^"]+\.css)"/g)].map((m) => m[1])
    if (hrefs.length === 0) return null
    return hrefs.find((h) => /\/index-[^/]*\.css$/.test(h)) ?? hrefs[0]
  } catch {
    return null
  }
}

/**
 * Adopt the dashboard's theme in this window: its variables, and its active theme id.
 *
 * Resolves once the stylesheet has loaded (or immediately if it cannot be found), so
 * a caller can render after the variables exist and avoid a flash of fallback colour.
 */
export async function adoptDashboardTheme(): Promise<void> {
  applyThemeId()
  keepWindowTransparent()

  if (document.getElementById(LINK_ID)) return

  const href = await findDashboardStylesheet()
  if (!href) return // the panel's own fallbacks stand in

  await new Promise<void>((resolve) => {
    const link = document.createElement('link')
    link.id = LINK_ID
    link.rel = 'stylesheet'
    link.href = href
    // Resolve either way: a missing stylesheet must not stop the companion appearing.
    link.onload = () => resolve()
    link.onerror = () => resolve()
    document.head.appendChild(link)
  })

  // Last word on transparency, after the dashboard's body rules are in play.
  keepWindowTransparent()
}

/**
 * Set `data-theme`, which is what the stylesheet keys its variable blocks on.
 *
 * Read from the same localStorage keys the dashboard writes, which works because the
 * window is same-origin. Split out so it can also run on a `storage` event, when the
 * user changes theme while the companion is open.
 */
export function applyThemeId(): void {
  try {
    const color = localStorage.getItem('mc-color-theme') || 'kiro'
    const pref = localStorage.getItem('mc-theme') || 'system'
    const mode = pref === 'system'
      ? (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
      : pref
    // `emerald` is the unprefixed default; every other theme is `<name>-<mode>`.
    document.documentElement.dataset.theme = color === 'emerald' ? mode : `${color}-${mode}`
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
