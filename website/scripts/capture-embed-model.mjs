/**
 * Screenshot harness for the Embedding Model card (Memory tab).
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call intercepted and answered from fixtures —
 * no gateway, no dashboard token. Same technique as capture-overview.mjs.
 *
 * Driving the fixtures (rather than a live re-embed) is what makes the
 * mid-flight states capturable at all: `running` with a partial count and
 * `failed` are otherwise timing-dependent and would be luck to catch.
 *
 * Captures:
 *   embed-01-bundled.png       idle, bundled model (no custom path set)
 *   embed-02-custom.png        idle, custom model active
 *   embed-03-validated.png     path typed + validated OK
 *   embed-04-invalid.png       path rejected (localized, from the error code)
 *   embed-05-confirm.png       the Apply confirmation modal
 *   embed-06-loading.png       applying — loading the model, NO percentage
 *   embed-07-running.png       re-embedding with a determinate bar + counts
 *   embed-08-failed.png        re-embed failed, counts retained
 *
 * Usage: node scripts/capture-embed-model.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/embed-shots'
const PORT = 6811
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' }
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  // Containment: resolve inside DIST and reject anything that escapes it.
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) {
    res.writeHead(403); res.end(); return
  }
  if (!existsSync(file) || path === '/') file = join(DIST, 'index.html')
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch { res.writeHead(404); res.end() }
})
await new Promise(r => server.listen(PORT, '127.0.0.1', r))

// Mutable fixture state the test drives between screenshots.
let embedStatus = {
  enabled: true, provider: 'local', model_available: true, server_healthy: true,
  setup_step: 'ready', model_id: 'all-MiniLM-L6-v2', model_dim: 384,
  model_source: 'default', model_path: '',
  reembed: { step: 'idle', done: 0, total: 0, error: '' },
}
let validateReply = { status: 200, body: { ok: true, size_bytes: 486_539_264 } }

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1240, height: 1000 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()
await page.routeWebSocket(/\/api\/ws/, () => {})

const unmatched = new Set()
await page.route('**/api/**', async route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/kiro-prerequisite') return json(route, {
    platform: 'gateway', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: true,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { status: 'idle', message: '' },
  })
  if (path === '/api/memory/embedding-status') return json(route, embedStatus)
  if (path === '/api/memory/embedding-model') return json(route, validateReply.body, validateReply.status)
  if (path === '/api/memory/stats') return json(route, { entries: 3148, size_bytes: 8_420_000, provider: 'local' })
  if (path === '/api/memory/settings') return json(route, { history_idle_hours: 3, history_max_days: 90, migrated: false })
  if (path === '/api/memory/preferences') return json(route, { content: '' })
  if (path === '/api/memory/projects') return json(route, { content: '' })
  if (path === '/api/memory/history') return json(route, { content: '' })
  if (path === '/api/memory/semantic') return json(route, { entries: [] })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '' })
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/status') return json(route, { sessions: 12, messages: 4821, cron_jobs: 7, subagents: 3, lessons: 52, uptime: 273840, version: '0.1.0' })
  if (path === '/api/chat/slots') return json(route, [])
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  const objectish = /(config|tips|voice|autonudge|branding|status|themes)/.test(path)
  unmatched.add(path)
  if (objectish) return json(route, {})
  return json(route, [])
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))
await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })

const settle = (ms = 1200) => page.waitForTimeout(ms)

/** The Memory tab is a DrillIn, not a route — reach it the way a user does:
 *  Overview -> the memory summary card's "View details". */
async function gotoMemory() {
  await page.goto(`http://127.0.0.1:${PORT}/settings?tab=overview`, { waitUntil: 'domcontentloaded' })
  await settle(2200)
  const details = page.getByRole('button', { name: 'View details' })
  await details.nth(1).click()
  await settle(1600)
}

/** Scroll the card into view and shoot just it, so the state is legible. */
async function shootCard(name) {
  const card = page.locator('[data-testid="embed-model-card"]')
  if (await card.count()) {
    await card.first().scrollIntoViewIfNeeded()
    await settle(400)
    await card.first().screenshot({ path: `${OUT}/${name}.png` })
  } else {
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
  }
}

// ---- 01 bundled / idle
await gotoMemory()
await shootCard('embed-01-bundled')

// ---- 02 custom model active
embedStatus = { ...embedStatus, model_id: 'bge-large-en-v1.5.gguf', model_dim: 1024, model_source: 'custom', model_path: '/home/you/models/bge-large-en-v1.5.gguf' }
await gotoMemory()
await shootCard('embed-02-custom')

// ---- 03 typed + validated OK
const input = page.getByPlaceholder('/path/to/your-model.gguf')
await input.fill('/home/you/models/nomic-embed-text-v1.5.gguf')
await input.blur()
await settle(1000)
await shootCard('embed-03-validated')

// ---- 04 rejected path (localized from the error code, not English prose)
validateReply = { status: 400, body: { ok: false, error: 'The model path points at a file that does not exist: /home/you/models/typo.gguf', code: 'model_path_not_found' } }
await input.fill('/home/you/models/typo.gguf')
await input.blur()
await settle(1000)
await shootCard('embed-04-invalid')

// ---- 05 confirm modal
validateReply = { status: 200, body: { ok: true, size_bytes: 486_539_264 } }
await input.fill('/home/you/models/nomic-embed-text-v1.5.gguf')
await input.blur()
await settle(900)
await page.getByRole('button', { name: /Apply model/i }).click()
await settle(900)
await page.screenshot({ path: `${OUT}/embed-05-confirm.png` })

// ---- 06 applying: loading the model, no denominator yet -> NO percentage
embedStatus = { ...embedStatus, reembed: { step: 'applying', done: 0, total: 0, error: '' } }
await page.keyboard.press('Escape')
await settle(600)
await gotoMemory()
await shootCard('embed-06-loading')

// ---- 07 running with counts
embedStatus = { ...embedStatus, model_id: 'nomic-embed-text-v1.5.gguf', model_dim: 768, model_source: 'custom', model_path: '/home/you/models/nomic-embed-text-v1.5.gguf', reembed: { step: 'running', done: 1842, total: 3148, error: '' } }
await gotoMemory()
await shootCard('embed-07-running')

// ---- 08 failed, counts retained
embedStatus = { ...embedStatus, reembed: { step: 'failed', done: 1842, total: 3148, error: 'the model could not be loaded' } }
await gotoMemory()
await shootCard('embed-08-failed')

console.log('unmatched /api paths:', [...unmatched].join(', ') || 'none')
await context.close()
await browser.close()
server.close()
console.log('done')
