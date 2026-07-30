/**
 * Shared boot-time API fixtures for the screenshot harnesses in this folder.
 *
 * Every harness runs the REAL built SPA with the network stubbed, so every one
 * of them has to answer the same set of boot calls before any scene-specific
 * route matters. Several of those answers are shape-sensitive in ways that are
 * not obvious (see the comments below) — keeping one copy here means a harness
 * that renders nothing is a bug in its own fixtures, not a re-discovery of
 * these.
 */

/** Fulfil `route` with a JSON body. */
export const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

/** Boot fixtures that are static across every scene, keyed by pathname. */
export function makeFixedApi(project) {
  return new Map([
    ['/api/status', { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' }],
    ['/api/notifications', { notifications: [], unread: 0 }],
    ['/api/auth/me', { user: 'owner', app: '' }],
    ['/api/models', { models: [], default: 'auto' }],
    ['/api/themes', { themes: [], installed: [] }],
    ['/api/dashboard/branding', { bot_name: 'Kiro', avatar: '' }],
    ['/api/recent-projects', { dirs: [project] }],
    ['/api/chat/nav/resolve-links', { summaries: [] }],
  ])
}

/**
 * Answer every boot call a harness does not handle itself.
 *
 * Call this as the LAST arm of a harness's `page.route('**\/api/**')` handler,
 * after its scene-specific routes. It always fulfils, so the route never hangs.
 */
export function handleBootRoute(route, path, { project, theme = 'dark', fixedApi }) {
  const fixed = fixedApi || makeFixedApi(project)
  // The setup gate runs BEFORE the app shell: an array-shaped stub makes it read
  // `.operation.status` off undefined and the whole shell error-boundaries out
  // with nothing rendered.
  if (path.startsWith('/api/kiro-prerequisite')) {
    return json(route, {
      platform: 'linux', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: false,
      repair_required: false, docs_url: '', setup_allowed: true,
      operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
    })
  }
  // The app shell iterates this on boot; an object-shaped stub throws inside the
  // ErrorBoundary and nothing renders at all.
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  if (fixed.has(path)) return json(route, fixed.get(path))
  if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' })
  const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
  return json(route, objectish ? {} : [])
}
