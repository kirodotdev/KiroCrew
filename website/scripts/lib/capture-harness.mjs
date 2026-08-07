/**
 * Shared plumbing for the screenshot capture harnesses.
 *
 * Each capture script (capture-agent-skills.mjs, capture-agent-template-create.mjs)
 * exercises ONE feature against the real built SPA with the network stubbed via
 * Playwright route interception. Everything that is not feature-specific lives
 * here: the static dist server, the JSON route helper, and the stubs for the
 * app-shell endpoints every page load hits regardless of feature.
 */
import { readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

// fileURLToPath, not URL.pathname: on Windows .pathname yields "/C:/…", which
// join() then turns into an invalid "\C:\…" and every read fails with ENOENT.
export const DIST = fileURLToPath(new URL('../../dist/', import.meta.url))

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

/** Static server with index.html fallback so /capabilities deep-links resolve. */
export function serveDist() {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '')
      let file = join(DIST, rel)
      if (!rel || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, 'index.html')
      res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' })
      res.end(readFileSync(file))
    })
    srv.listen(0, '127.0.0.1', () => resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }))
  })
}

export const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

/**
 * Answer the app-shell endpoints every page load hits, none of which are what
 * a capture is testing. Returns true when the route was handled; a script's
 * own handler runs first and falls through to this for everything else.
 */
export function stubCommonApi(route, path) {
  if (path === '/api/config/default-agent') return json(route, { default_agent: 'kirocrew' }), true
  if (path.startsWith('/api/agent-metadata/')) return json(route, { content: '' }), true
  if (path === '/api/spawn') return json(route, { agents: [] }), true
  if (path === '/api/sessions/context') return json(route, { sessions: [] }), true
  if (path === '/api/sessions/usage') return json(route, { usage: null }), true
  if (path === '/api/models') {
    return json(route, [
      { model_name: 'auto', description: 'Let Kiro choose' },
      { model_name: 'claude-opus-4.8', description: 'Most capable' },
      { model_name: 'claude-sonnet-4.5', description: 'Balanced' },
    ]), true
  }
  // The app shell mounts behind this gate and reads status.operation.status —
  // a generic object stub crashes it, blanking the whole page.
  if (path === '/api/kiro-prerequisite') {
    return json(route, {
      platform: 'linux', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: false,
      repair_required: false, docs_url: '', setup_allowed: false,
      operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
    }), true
  }
  if (path === '/api/chat/slots') return json(route, []), true
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' }), true
  if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, subagents: 0, uptime: 120, version: 'dev' }), true
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 }), true
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' }), true
  if (path === '/api/themes') return json(route, { themes: [], installed: [] }), true
  if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' }), true
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' }), true
  if (path === '/api/recent-projects') return json(route, { dirs: [] }), true
  if (path === '/api/dashboard/config') return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }), true
  const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
  return json(route, objectish ? {} : []), true
}
