/**
 * Screenshot harness for `backend_refused` on an app update (#27840).
 *
 * Runs the REAL built SPA (website/dist, or the dist named by the second
 * argument) on a static server with every /api/** call answered from fixtures.
 * The update endpoint answers 200 with `backend_refused`, the field the gateway
 * adds when the host refused the app's backend (a shell launcher on native
 * Windows). The enable and install surfaces read it through the same helper.
 *
 * Captures:
 *   detail-dark.png     the detail page after Sync, dark theme
 *   detail-light.png    the same, light theme
 *   detail-narrow.png   the same at a 390px phone width
 *   library-dark.png    the Library page after Sync on the card
 *
 * Usage: node scripts/capture-app-backend-refused.mjs <outDir> [distDir] [--before]
 * `--before` skips the "warning is shown" assertions, for a run against a base
 * build that predates the warning.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'

const args = process.argv.slice(2)
const BEFORE = args.includes('--before')
const [OUT = '/tmp/app-backend-refused-shots', DIST = DEFAULT_DIST] = args.filter(a => a !== '--before')
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist(DIST)

const REASON = "App 'build-watch' has an exec (shell launcher) backend, which is not supported on native Windows. Run Kiro Crew under WSL to use it."
const status = { sessions: 4, messages: 812, cron_jobs: 1, subagents: 0, lessons: 12, uptime: 91200, version: '0.1.0' }
const json = (route, body, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(body) })

/** Installed from a directory, so Sync updates it in place on both pages. */
const APP = {
  name: 'build-watch',
  displayName: 'Build Watch',
  version: '0.2.0',
  enabled: true,
  installedAt: '2026-10-30T09:00:00Z',
  source: 'C:\\Users\\dev\\apps\\build-watch',
  origin: 'local',
  resources: 'gateway',
  lifecycle: 'gateway',
  manifest: {
    name: 'build-watch', version: '0.2.0', displayName: 'Build Watch',
    description: 'Watches local builds and posts the result to your chat.',
    author: 'dev', tags: ['developer-tools'],
    backend: { type: 'exec', entry: 'run.sh' },
  },
}

const updateCalls = []

async function run(mode, viewport) {
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await context.newPage()
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    const m = path.match(/^\/api\/apps\/([^/]+)\/update$/)
    if (m && method === 'POST') {
      updateCalls.push(m[1])
      return json(route, { ok: true, name: m[1], backend_refused: REASON })
    }
    if (path === '/api/apps/registry') return json(route, { apps: [], serverPlatform: { os: 'windows', arch: 'x64' } })
    if (path === '/api/apps/registries') return json(route, { registries: [] })
    if (path === `/api/apps/${APP.name}`) return json(route, APP)
    if (path === '/api/apps') return json(route, [APP])
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/kiro-prerequisite') return json(route, {
      platform: 'gateway', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: true,
      repair_required: false, docs_url: '', setup_allowed: false,
      operation: { status: 'idle', message: '' },
    })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/status') return json(route, status)
    if (path === '/api/system') return json(route, { hostname: 'dev-host' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
    if (path === '/api/theme/boot') return json(route, { mode, theme: '' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/chat/slots') return json(route, [])
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (/(config|tips|voice|autonudge|branding|status|themes|trusted)/.test(path)) return json(route, {})
    return json(route, [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))
  await page.addInitScript(m => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme-mode', m)
  }, mode)
  const settle = async (ms = 1400) => {
    await page.waitForTimeout(ms)
    if (wsServer) wsServer.send(JSON.stringify({ type: 'status', data: status }))
    await page.waitForTimeout(500)
  }
  return { browser, context, page, settle }
}

/** Fail loudly: a capture of the wrong state is worse than no capture. */
function must(cond, msg) {
  if (!cond) { console.log('HARNESS ASSERTION FAILED:', msg); process.exitCode = 1 }
}

async function detail(file, mode, viewport) {
  const { browser, page, settle } = await run(mode, viewport)
  await page.goto(`${base}/apps/detail/${APP.name}`, { waitUntil: 'domcontentloaded' })
  await settle(2000)
  const sync = page.getByRole('button', { name: 'Sync' })
  must(await sync.count() >= 1, `${file}: expected a Sync button on the detail page`)
  const before = updateCalls.length
  await sync.first().click()
  await settle(1600)
  must(updateCalls.length > before, `${file}: Sync must hit the update endpoint`)
  const warned = await page.getByText(/backend of .* did not start/).count()
  const succeeded = await page.getByText(/from its source directory/).count()
  if (BEFORE) console.log(`${file}: warning=${warned} success=${succeeded}`)
  else {
    must(warned >= 1, `${file}: the detail page must show the backend warning`)
    must(succeeded === 0, `${file}: the detail page must not also show the success notice`)
  }
  await page.screenshot({ path: `${OUT}/${file}` })
  await browser.close()
}

async function library(file, mode, viewport) {
  const { browser, page, settle } = await run(mode, viewport)
  await page.goto(`${base}/apps/library`, { waitUntil: 'domcontentloaded' })
  await settle(2400)
  // The launchpad card keeps Sync in its overflow menu.
  await page.locator('button[aria-haspopup]').last().click()
  await settle(600)
  const sync = page.getByRole('menuitem', { name: 'Sync' })
  must(await sync.count() >= 1, `${file}: expected Sync in the Library card menu`)
  await sync.first().click()
  await settle(1600)
  const warned = await page.getByText(/backend of .* did not start/).count()
  const succeeded = await page.getByText(/from its source directory/).count()
  if (BEFORE) console.log(`${file}: warning=${warned} success=${succeeded}`)
  else {
    must(warned >= 1, `${file}: the Library page must show the backend warning`)
    must(succeeded === 0, `${file}: the Library page must not also show the success notice`)
  }
  await page.screenshot({ path: `${OUT}/${file}` })
  await browser.close()
}

const WIDE = { width: 1280, height: 800 }
await detail('detail-dark.png', 'dark', WIDE)
await detail('detail-light.png', 'light', WIDE)
await detail('detail-narrow.png', 'dark', { width: 390, height: 844 })
await library('library-dark.png', 'dark', { width: 1520, height: 1000 })

srv.close()
console.log(process.exitCode ? 'FAILED' : 'done')
