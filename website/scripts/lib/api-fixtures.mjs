/**
 * Shared /api/** fixture router for the screenshot harnesses.
 *
 * Every capture script that shoots a real page needs the same boot-path
 * responses (prerequisite gate, slots, status, theme, branding …) before the SPA
 * will render anything at all. Two harnesses carrying byte-identical copies of
 * that table trips `npm run jscpd`, which is wired as `pretest` at a 0%
 * threshold — so the duplication does not merely read badly, it fails the test
 * command outright.
 *
 * Callers pass only the routes they actually care about via `overrides`; the
 * default table answers the rest. Anything unmatched falls through to `[]` (or
 * `{}` for object-shaped endpoints), which is what the dashboard's queries
 * tolerate.
 */

/** Fulfill a Playwright route with a JSON body. */
export const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

/** The boot-path responses every harness needs to get a rendered page. */
const DEFAULTS = {
  '/api/kiro-prerequisite': {
    platform: 'darwin', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
  },
  '/api/chat/slots': [],
  '/api/status': { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: '0.5.0' },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/themes': { themes: [], installed: [] },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  // Two WORDS, matching the backend's own default (`api_branding`:
  // `cfg.dashboard.bot_name or "Kiro Crew"`). The nav brand row accents the last
  // word only and the composer placeholder interpolates the whole name, so a
  // single-word "Kiro" here silently shot every harness in this folder with no
  // "CREW" in the top-left. Fixed once in stub-dashboard-api.mjs; this table was
  // extracted later and did not carry it over.
  '/api/dashboard/branding': { bot_name: 'Kiro Crew', avatar: '' },
  '/api/recent-projects': { dirs: [] },
  '/api/dashboard/config': {
    restore_sessions: false, restore_window_minutes: 30,
    merge_queued_messages: false, widget_density: 'more',
  },
}

/**
 * Install the fixture router on a page, plus the WS stub the dashboard opens on
 * mount (left unanswered, so no status pushes race the shot).
 *
 * @param {import('playwright').Page} page
 * @param {Record<string, unknown>} [overrides] pathname -> body, wins over DEFAULTS
 */
export async function installApiFixtures(page, overrides = {}) {
  await page.routeWebSocket(/\/api\/ws/, () => {})

  const table = { ...DEFAULTS, ...overrides }

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (Object.prototype.hasOwnProperty.call(table, path)) return json(route, table[path])
    // Prefix match for the collection endpoints that carry an id segment.
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    // Object-shaped endpoints must not receive an array, or destructuring throws.
    if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) return json(route, {})
    return json(route, [])
  })
}

/** Log page errors and console errors — a silent white page is the failure mode. */
export function logPageFailures(page) {
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })
}
