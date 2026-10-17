/**
 * Screenshot harness for the composer's model/effort capsule on a BRAND-NEW
 * session — the surface in the reported bug.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route
 * interception. No gateway, no dashboard credential, no kiro-cli spawn.
 *
 * The scenario: Settings → Chat is configured with claude-opus-5 / High, the
 * kiro agent file pins claude-opus-4.8, and the slot has run no turns yet so it
 * carries neither a model nor an effort override. The composer must advertise
 * what the turn WILL run on (the configured default), not the agent file's
 * model — the backend resolves `agent.model` for the builtin agent and
 * `slot.reasoning_effort or agent.reasoning_effort` for effort.
 *
 * Usage: node scripts/capture-model-display.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const OUT = process.argv[2] || '../temp-screenshots/model-display'
const DIST = fileURLToPath(new URL('../dist/', import.meta.url))
const SLOT = 'chat-fresh-session'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

function serveDist() {
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

const MODELS = [
  { model_name: 'auto', description: 'Let Kiro choose' },
  { model_name: 'claude-opus-5', description: 'Most capable' },
  { model_name: 'claude-opus-4.8', description: 'Previous flagship' },
  { model_name: 'claude-sonnet-4.6', description: 'Balanced' },
]

// Flipped per scene. `agentFileModel` is what ~/.kiro/agents/kirocrew.json pins —
// the value the composer used to show in place of the configured default.
const scene = { model: 'claude-opus-5', effort: 'high', agentFileModel: 'claude-opus-4.8' }

// A session that has never run a turn: no model, no effort override. This is
// exactly the state the backend fills in from config at spawn time.
const freshSlot = {
  key: SLOT,
  title: 'New session',
  running: false,
  last_message: '',
  messages: 0,
  agent: 'default',
  memory_mode: 'persistent',
  project: PROJECT,
  model: '',
  reasoning_effort: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}

const detail = {
  running: false, has_more: false, total: 0, queue: [], project: PROJECT,
  model: '', reasoning_effort: '', messages: [],
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname

    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: { model: scene.model, reasoning_effort: scene.effort, provider: 'acp' },
        session: { autocompact_pct: 90 },
        dashboard: { user_role: '', user_technical_level: '' },
      })
    }
    if (path === '/api/models') return json(route, MODELS)
    if (path === '/api/effort-levels' || path.startsWith('/api/effort-levels')) {
      return json(route, ['low', 'medium', 'high', 'xhigh', 'max'])
    }
    // The builtin agent + its kiro template file, which pins a DIFFERENT model.
    if (path === '/api/agents') {
      return json(route, {
        agents: [{ name: 'default', kiro_agent: 'kirocrew', description: 'Default crew agent' }],
        default_agent: 'default',
      })
    }
    if (path.startsWith('/api/agents/detail/')) {
      return json(route, { name: 'kirocrew', model: scene.agentFileModel, skills: [] })
    }
    if (path === '/api/agents/installed') return json(route, [])
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/chat/slots') return json(route, [freshSlot])
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    if (path === '/api/dashboard/config') {
      return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
    }
    if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  async function load() {
    await page.addInitScript(slot => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', slot)
    }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3200)
  }

  const capsule = () => page.locator('[title^="Model:"]').first()

  /** Crop the composer footer: the capsule plus the input above it. */
  async function shotComposer(name) {
    const box = await capsule().boundingBox()
    if (!box) return page.screenshot({ path: `${OUT}/${name}.png` })
    const x = Math.max(0, box.x - 1000)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x, y: Math.max(0, box.y - 150), width: Math.min(1500 - x, box.x + box.width + 60 - x), height: 200 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  await load()
  await capsule().waitFor({ timeout: 25000 })
  const text = (await capsule().innerText()).replace(/\s+/g, ' ').trim()
  console.log('CAPSULE:', JSON.stringify(text))
  await shotComposer('01-composer-fresh-session')

  // Open the picker and drill into the effort page.
  await capsule().click()
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/02-model-picker.png` })
  console.log('wrote', `${OUT}/02-model-picker.png`)

  const reasoning = page.getByRole('button', { name: /^Reasoning/ }).first()
  if (await reasoning.count()) {
    console.log('PICKER_FOOTER:', JSON.stringify((await reasoning.innerText()).replace(/\s+/g, ' ').trim()))
    await reasoning.click()
    await page.waitForTimeout(900)
    await page.screenshot({ path: `${OUT}/03-effort-page.png` })
    console.log('wrote', `${OUT}/03-effort-page.png`)
  } else {
    console.log('PICKER_FOOTER: <absent>')
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
