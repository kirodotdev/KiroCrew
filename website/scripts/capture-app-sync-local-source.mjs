/**
 * Screenshot harness for the Apps-page Sync dispatch.
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call answered from fixtures — no gateway, no
 * dashboard token. Same technique as capture-apps.mjs.
 *
 * The surface under test is which endpoint Sync reaches, so the fixtures make
 * that observable: the update endpoint records the calls it received, and the
 * registry install stream is stubbed to fail the way it does for an app no
 * registry lists.
 *
 * Captures:
 *   library-path-app.png   a path-installed app's card, offering Sync
 *   sync-in-place.png      after Sync — still on Apps, no registry error
 *   sync-failure.png       a failed in-place sync, reported on the card's page
 *   registry-app-detail.png a registry-sourced app still routes to the
 *                           streaming detail page
 *
 * Usage: node scripts/capture-app-sync-local-source.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/app-sync-shots'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()

const status = { sessions: 4, messages: 812, cron_jobs: 1, subagents: 0, lessons: 12, uptime: 91200, version: '0.1.0' }
const json = (route, body, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(body) })

/** Installed from a DIRECTORY — the case that used to fail its own Sync. */
const PATH_APP = {
  name: 'orchestrator-switch',
  displayName: 'Orchestrator Switch',
  version: '0.1.0',
  enabled: true,
  installedAt: '2026-08-18T09:00:00Z',
  source: '/home/dev/workspace/orchestrator-switch',
  origin: 'local',
  resources: 'gateway',
  lifecycle: 'gateway',
  manifest: {
    name: 'orchestrator-switch', version: '0.1.0', displayName: 'Orchestrator Switch',
    description: "Toggle Kiro Crew's orchestrator between the Claude Code companion and stock kiro-cli, restart the gateway to apply the flip, and see what is actually configured vs live.",
    author: 'kirocrew-claude-companion', tags: ['developer-tools'],
    ui: { pages: [{ route: '/orchestrator-switch', label: 'Orchestrator Switch', icon: 'Boxes' }] },
  },
}

/** Installed from a REGISTRY — must keep routing at the streaming detail page. */
const REGISTRY_APP = {
  name: 'secretary',
  displayName: 'Secretary',
  version: '1.0.0',
  enabled: true,
  installedAt: '2026-07-20T10:00:00Z',
  source: 'registry:secretary',
  origin: 'registry',
  resources: 'gateway',
  lifecycle: 'gateway',
  manifest: {
    name: 'secretary', version: '1.0.0', displayName: 'Secretary',
    description: 'Slack inbox manager — triage, draft replies, and digest channels.',
    author: 'zezhexu', tags: ['slack'],
  },
}

// Observable state the captures depend on.
const updateCalls = []
let updateShouldFail = false
let installStreamCalls = 0

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1520, height: 1000 }, deviceScaleFactor: 2 })
const page = await context.newPage()

let wsServer = null
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

const unmatched = new Set()
await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  const method = route.request().method()

  // The endpoint this fix routes a path-installed app to.
  const m = path.match(/^\/api\/apps\/([^/]+)\/update$/)
  if (m && method === 'POST') {
    updateCalls.push(m[1])
    if (updateShouldFail) {
      return json(route, { error: 'source path no longer exists: /home/dev/workspace/orchestrator-switch' }, 400)
    }
    return json(route, { ok: true, name: m[1] })
  }
  // The registry path. Answers a REAL SSE stream ending in a `done` frame:
  // this scene is the "unchanged, still works" evidence, and a failed install
  // in that frame reads as the reported bug still being present. A plain JSON
  // body is not enough — the client parses SSE and reports "Stream ended
  // without completion" for anything else. The dispatch assertions below prove
  // the routing; the frame only has to show the path is healthy.
  if (path === '/api/apps/registry/install-stream' || path === '/api/apps/registry/install') {
    installStreamCalls += 1
    const done = JSON.stringify({ ok: true, name: REGISTRY_APP.name })
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: `event: log\ndata: cloning ${REGISTRY_APP.name}…\n\n`
        + 'event: log\ndata: build succeeded\n\n'
        + `event: done\ndata: ${done}\n\n`,
    })
  }
  if (path === '/api/apps/registry') return json(route, { apps: [], serverPlatform: { os: 'linux', arch: 'x64' } })
  if (path === '/api/apps/registries') return json(route, { registries: [] })
  if (path === `/api/apps/${PATH_APP.name}`) return json(route, PATH_APP)
  if (path === `/api/apps/${REGISTRY_APP.name}`) return json(route, REGISTRY_APP)
  if (path === '/api/apps') return json(route, [PATH_APP, REGISTRY_APP])
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
  if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/chat/slots') return json(route, [])
  if (path === '/api/models') return json(route, { models: [], default: 'auto' })
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  const objectish = /(config|tips|voice|autonudge|branding|status|themes|trusted)/.test(path)
  unmatched.add(path)
  if (objectish) return json(route, {})
  return json(route, [])
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-theme-mode', 'dark')
})

const pushStatus = () => wsServer && wsServer.send(JSON.stringify({ type: 'status', data: status }))
async function settle(ms = 1400) { await page.waitForTimeout(ms); pushStatus(); await page.waitForTimeout(500) }

/** Fail loudly: a capture of the wrong state is worse than no capture. */
function must(cond, msg) {
  if (!cond) { console.log('HARNESS ASSERTION FAILED:', msg); process.exitCode = 1 }
}

// ---- Library: the path-installed app's card, offering Sync
await page.goto(`${base}/apps`, { waitUntil: 'domcontentloaded' })
await settle(2400)
await page.getByText('Library').first().click()
await settle(1600)
await page.screenshot({ path: `${OUT}/library-path-app.png` })

// ---- Sync on the path-installed card: reaches the update endpoint, stays put
const syncButtons = page.getByRole('button', { name: 'Sync' })
must(await syncButtons.count() >= 1, 'expected a Sync button on the Library card')
await syncButtons.first().click()
await settle(1600)
await page.screenshot({ path: `${OUT}/sync-in-place.png` })
must(updateCalls.includes(PATH_APP.name), `expected POST /api/apps/${PATH_APP.name}/update, saw ${JSON.stringify(updateCalls)}`)
must(!page.url().includes('/apps/detail/'), `expected to stay on /apps, url is ${page.url()}`)
must(installStreamCalls === 0, 'the registry install stream must not be reached for a path-installed app')

// ---- A failed in-place sync is reported where the user clicked
updateShouldFail = true
await page.getByRole('button', { name: 'Sync' }).first().click()
await settle(1600)
await page.screenshot({ path: `${OUT}/sync-failure.png` })
updateShouldFail = false

// ---- A registry-sourced app still routes to the streaming detail page
await page.goto(`${base}/apps`, { waitUntil: 'domcontentloaded' })
await settle(2000)
await page.getByText('Library').first().click()
await settle(1400)
const before = updateCalls.length
const rows = page.getByRole('button', { name: 'Sync' })
const count = await rows.count()
must(count >= 2, `expected a Sync button on both cards, saw ${count}`)
await rows.nth(1).click()
await settle(2000)
await page.screenshot({ path: `${OUT}/registry-app-detail.png` })
must(page.url().includes('/apps/detail/'), `expected the registry app to route to the detail page, url is ${page.url()}`)
must(updateCalls.length === before, 'the registry app must NOT hit the in-place update endpoint')

console.log('update endpoint calls:', JSON.stringify(updateCalls))
console.log('registry install stream calls:', installStreamCalls)
must(installStreamCalls >= 1, 'the registry app must actually reach the registry install stream (otherwise the "not reached" assertion above is vacuous)')

// ---- The detail page's OWN Sync button: the second surface this fix wires.
// It is not gated on source, so it is where the provenance wording has to be
// right, and the success notice here is the one this page previously lacked.
// Captured on the PATH-installed app, so the source-directory wording is the
// truthful one for this frame.
await page.goto(`${base}/apps/detail/${PATH_APP.name}`, { waitUntil: 'domcontentloaded' })
await settle(2000)
const detailBefore = updateCalls.length
const detailSync = page.getByRole('button', { name: 'Sync' })
must(await detailSync.count() >= 1, 'expected a Sync button on the detail page')
await detailSync.first().click()
await settle(1600)
await page.screenshot({ path: `${OUT}/detail-sync-success.png` })
must(updateCalls.length > detailBefore, 'the detail-page Sync must hit the in-place update endpoint')
must(
  await page.getByText(/from its source directory/).count() >= 1,
  'the detail page must state the sync succeeded (otherwise this frame proves nothing)',
)

console.log('unmatched /api paths:', [...unmatched].join(', ') || 'none')
await context.close()
await browser.close()
srv.close()
console.log(process.exitCode ? 'FAILED' : 'done')
