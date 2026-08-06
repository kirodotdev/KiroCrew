/**
 * Shared /api/** fixture stub for the screenshot harnesses in this folder.
 *
 * Every harness runs the REAL built SPA gateway-free, which means every harness
 * needs the same ~25 endpoint stubs just to get the dashboard to boot: the
 * prerequisite gate, theme/branding, auth, status, notifications. Only the
 * folders and slots differ per harness, so keeping the boot fixtures here means
 * one copy instead of one per capture script (jscpd flags the duplication, and
 * more importantly a new endpoint added to the boot path had to be patched into
 * every script by hand).
 *
 * `extra` lets a harness override or add a route without forking the whole map:
 * it is consulted FIRST, and returning a truthy value from a handler marks the
 * request handled.
 */

/** Fulfil a Playwright route with a JSON body. */
export const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

/**
 * Install the gateway-free API stub on a page.
 *
 * @param {import('playwright').Page} page
 * @param {{
 *   folders?: unknown[],
 *   slots?: unknown[],
 *   theme?: string,
 *   botName?: string,
 *   extra?: (path: string, route: import('playwright').Route) => unknown,
 * }} opts
 */
export async function stubDashboardApi(page, opts = {}) {
  const {
    folders = [],
    slots = [],
    theme = 'dark',
    // The backend's own default (`api_branding`: `cfg.dashboard.bot_name or
    // "Kiro Crew"`). It must stay TWO WORDS: the nav brand row accents the last
    // word only, and the composer placeholder interpolates the whole name — so
    // a single-word "Kiro" here silently produced screenshots with no "CREW"
    // and a "Message Kiro…" composer, in every harness in this folder.
    botName = 'Kiro Crew',
    extra = null,
  } = opts

  // Swallow the dashboard's websocket so it does not retry-storm with no gateway.
  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname

    if (extra && (await extra(path, route))) return

    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'darwin', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/chat/folders') return json(route, folders)
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') {
      return json(route, { sessions: slots.length, crons: 0, lessons: 0, uptime: 120, version: '0.5.0' })
    }
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' })
    // Mirrors `api_branding` exactly, avatar included. Returning '' let the
    // frontend fall back to its own '/logo.png' default, which is the same URL —
    // but being explicit keeps the fixture readable as "what the server sends".
    if (path === '/api/dashboard/branding') return json(route, { bot_name: botName, avatar: '/logo.png' })
    if (path === '/api/recent-projects') return json(route, { dirs: [] })
    if (path === '/api/dashboard/config') {
      return json(route, {
        restore_sessions: false, restore_window_minutes: 30,
        merge_queued_messages: false, widget_density: 'more',
      })
    }
    if (path === '/api/agents' || path === '/api/chat/agents') {
      return json(route, [{ name: 'kirocrew', source: 'builtin' }, { name: 'oncall', source: 'aim' }])
    }
    // Endpoints not worth naming individually: anything object-shaped gets {},
    // everything else gets []. Guessing wrong only costs an empty panel.
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })

  await page.addInitScript(([themeMode]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', themeMode)
    localStorage.setItem('mc-onboarded', '1')
  }, [theme])
}

/** Surface page errors + console errors on stdout so a broken capture is obvious. */
export function logPageProblems(page) {
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })
}
