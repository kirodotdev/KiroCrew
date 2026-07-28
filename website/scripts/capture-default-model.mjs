/**
 * Screenshot harness for the default model / default reasoning effort settings.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback, and answers every /api/** call from fixtures via Playwright
 * route interception. No gateway, no dashboard token, no kiro-cli spawn.
 *
 * The client code under test is unmodified — only the network is stubbed — so
 * Settings > Chat > Model, the two selects, and the model picker's
 * "Set default for new sessions…" footer link are exercised as they run in
 * production. The stored config is flipped between scenes so the effort row can
 * be captured both live (reasoning-capable model) and inert (auto).
 *
 * Usage: node scripts/capture-default-model.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const OUT = process.argv[2] || '../temp-screenshots/default-model'
// fileURLToPath, not URL.pathname: on Windows .pathname yields "/C:/…", which
// join() then turns into an invalid "\C:\…" and every read fails with ENOENT.
const DIST = fileURLToPath(new URL('../dist/', import.meta.url))
const SLOT = 'chat-default-model'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

/** Static server with index.html fallback so /settings deep-links resolve. */
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

/** The model list /api/models would return from a live kiro-cli. */
const MODELS = [
  { model_name: 'auto', description: 'Let Kiro choose' },
  { model_name: 'claude-opus-4.8', description: 'Most capable' },
  { model_name: 'claude-sonnet-4.5', description: 'Balanced' },
  { model_name: 'claude-haiku-4.5', description: 'Fastest' },
  { model_name: 'gpt-5.6-sol', description: 'GPT-5.6' },
]

/** Flipped per-scenario: the persisted config the Settings panel reads back. */
const scene = { model: 'auto', effort: '', theme: 'dark' }

const slots = [{
  key: SLOT,
  title: 'Add pagination to /users',
  running: false,
  last_message: 'Added limit/offset plus tests.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  model: 'claude-opus-4.8',
  reasoning_effort: 'high',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false, has_more: false, total: 2, queue: [], project: PROJECT,
  model: 'claude-opus-4.8', reasoning_effort: 'high',
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Add pagination to the /users endpoint.' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Added `limit`/`offset` to the query layer plus 4 tests.' },
  ],
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // Settings rows are 12–13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()

    // The PATCH the two selects issue — echo success and let the panel refetch.
    if (path === '/api/config/kirocrew' && method === 'PATCH') {
      const body = JSON.parse(route.request().postData() || '{}')
      if (body.path === 'agent.model') scene.model = body.value
      if (body.path === 'agent.reasoning_effort') scene.effort = body.value
      return json(route, { ok: true })
    }
    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: {
          model: scene.model,
          reasoning_effort: scene.effort,
          completion_keep: 'head',
          completion_keep_chars: 3000,
          soft_stop_budget_secs: 10,
        },
        session: { autocompact_pct: 90 },
        dashboard: { user_role: '', user_technical_level: '' },
      })
    }
    if (path === '/api/models') return json(route, MODELS)
    // The app shell mounts behind this gate and reads status.operation.status —
    // the generic object stub crashes it, blanking the whole page.
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: scene.theme, theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    if (path === '/api/dashboard/config') return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  async function load(url, theme = 'dark') {
    scene.theme = theme
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-default-model')
    }, theme)
    await page.goto(base + url, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Tight crop around the Model card — the whole story for the settings shots.
   *  Derived from the two rows' real boxes so long descriptions never clip. */
  async function card(name, pad = 20) {
    const heading = page.getByRole('heading', { name: 'Model', exact: true })
    const rows = page.locator('[data-setting-label="Default Model"], [data-setting-label="Default Reasoning Effort"]')
    if (await heading.count() && await rows.count()) {
      const hb = await heading.first().boundingBox()
      const boxes = []
      for (let i = 0; i < await rows.count(); i++) boxes.push(await rows.nth(i).boundingBox())
      const valid = boxes.filter(Boolean)
      if (hb && valid.length) {
        const x0 = Math.min(hb.x, ...valid.map(b => b.x)) - pad
        const y0 = hb.y - pad
        const x1 = Math.max(hb.x + hb.width, ...valid.map(b => b.x + b.width)) + pad
        const y1 = Math.max(...valid.map(b => b.y + b.height)) + pad
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x: Math.max(0, x0),
            y: Math.max(0, y0),
            width: Math.min(1500 - Math.max(0, x0), x1 - Math.max(0, x0)),
            height: y1 - Math.max(0, y0),
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    await shot(name)
  }

  // 1. Default state: model = auto, so the effort row is present but inert.
  scene.model = 'auto'; scene.effort = ''
  await load('/settings?tab=chat')
  await shot('01-settings-chat-model-auto-dark')
  await card('02-model-card-auto-dark')

  // 2. A reasoning-capable default: the effort row activates.
  scene.model = 'claude-opus-4.8'; scene.effort = 'xhigh'
  await load('/settings?tab=chat')
  await card('03-model-card-opus-xhigh-dark')

  // 3. The Default Model dropdown open over the live model list.
  await page.getByRole('combobox', { name: 'Default Model' }).click()
  await page.waitForTimeout(700)
  await shot('04-default-model-dropdown-dark')

  // 4. The effort dropdown, including the "Model default" sentinel.
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)
  await page.getByRole('combobox', { name: 'Default Reasoning Effort' }).click()
  await page.waitForTimeout(700)
  await shot('05-default-effort-dropdown-dark')

  // 5. The in-session model picker: the new footer link sits under the list,
  //    beside the existing Reasoning footer.
  await load('/')
  const modelBtn = page.locator('button').filter({ hasText: /claude-opus-4\.8|auto/ }).last()
  if (await modelBtn.count()) {
    await modelBtn.click()
    await page.waitForTimeout(900)
    const link = page.getByText(/Set default for new sessions/)
    if (await link.count()) {
      const box = await link.first().boundingBox()
      if (box) {
        await page.screenshot({
          path: `${OUT}/06-picker-set-default-link-dark.png`,
          clip: {
            x: Math.max(0, box.x - 30),
            y: Math.max(0, box.y - 330),
            width: Math.min(1500 - Math.max(0, box.x - 30), 420),
            height: 390,
          },
        })
        console.log('wrote', `${OUT}/06-picker-set-default-link-dark.png`)
      }
    } else {
      console.log('NOTE: footer link not found — capturing full frame')
    }
    await shot('07-picker-open-dark')
  } else {
    console.log('NOTE: model button not found in composer')
  }

  // 6. The deep link the footer navigates to. The highlight ring self-clears
  //    after ~2s, so this scene loads with a shorter settle than the others.
  scene.model = 'claude-opus-4.8'; scene.effort = 'xhigh'
  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
  })
  await page.goto(base + '/settings?tab=chat&highlight=chat.default-model', { waitUntil: 'domcontentloaded' })
  await page.getByRole('combobox', { name: 'Default Model' }).waitFor({ timeout: 15000 })
  await page.waitForTimeout(350)
  await card('08-deeplink-highlight-dark', 26)

  // 7. Light-theme parity.
  await load('/settings?tab=chat', 'light')
  await card('09-model-card-opus-light')

  scene.model = 'auto'; scene.effort = ''
  await load('/settings?tab=chat', 'light')
  await card('10-model-card-auto-light')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
