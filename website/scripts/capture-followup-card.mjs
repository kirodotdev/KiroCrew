/**
 * Screenshot harness for the follow-up suggestion card.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call and the /api/ws websocket intercepted by Playwright and answered
 * from fixtures. No gateway, no dashboard token, no git, no worktrees created.
 *
 * The client code under test is unmodified — only the network is stubbed — so the
 * card, its three actions, and the composer prefill are exercised exactly as they
 * run in production. The card is driven the way the backend drives it: by pushing
 * a `followup_card` frame into the live websocket after the page has rendered.
 *
 * Usage: node scripts/capture-followup-card.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6802'
const OUT = process.argv[3] || '../temp-screenshots/followup-suggest'
const SLOT = 'chat-followup'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Add rate limiting to uploads',
  running: false,
  last_message: 'Added the token-bucket limiter and its tests.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: Date.now() / 1000 - 600,
      content: 'Add a rate limiter to the upload endpoint.',
    },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 30,
      content:
        'Added a token-bucket limiter to `POST /api/upload` plus 6 tests covering the ' +
        'refill window and the 429 path. All gates green.',
    },
  ],
}

/** The three suggestions the agent would author for that turn. */
const ITEMS = [
  {
    title: 'Add rate limiting to the WebSocket upgrade path',
    description:
      'The upload endpoint is bounded now, but /api/ws still accepts unlimited concurrent upgrades from one caller.',
    prompt:
      'In src/kiro_crew/dashboard/ws.py, apply the same token-bucket limiter added to the upload handler to the WebSocket upgrade path. Reuse the limiter helper rather than duplicating it, cap concurrent sockets per caller, and add tests for the reject path.',
    branch: 'feat/ws-rate-limit',
  },
  {
    title: 'Surface the 429 in the dashboard toast',
    description:
      'A rejected upload currently fails silently in the UI — the user sees nothing.',
    prompt:
      'When an upload returns 429, render the retry-after value in a dashboard toast instead of failing silently. Touch website/src/api/client.ts and the upload call site, and add a vitest case.',
  },
  {
    title: 'Document the limiter defaults',
    description: 'The new limits are undocumented, so operators cannot tune them.',
    prompt:
      'Document the upload rate-limiter defaults and their config keys in src/kiro_crew/docs/configuration.md, including how to raise them for a trusted deployment.',
    branch: 'docs/rate-limit-defaults',
  },
]

/** Flipped per-scenario so one run can capture both success and failure. */
const scene = { worktreeError: null, theme: 'dark' }

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The card is dense small type (12–13px); a 1x shot renders it soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/worktree/create') {
      if (scene.worktreeError) return json(route, { error: scene.worktreeError }, 409)
      return json(route, {
        ok: true,
        path: PROJECT + '-wt-ws-rate-limit',
        branch: 'feat/ws-rate-limit',
        base: 'origin/HEAD',
      })
    }
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // The app shell iterates this on boot; an object-shaped stub throws inside
    // the ErrorBoundary and nothing renders at all.
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: scene.theme, theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  async function load(theme) {
    // The boot endpoint wins over localStorage on first paint, so the stub has
    // to agree with the requested theme or every "light" shot renders dark.
    scene.theme = theme
    await page.addInitScript(t => {
      // Wipe first: the composer persists drafts to localStorage, so without
      // this the prefill scenario's text bleeds into every later scenario's
      // composer and misreads as that scenario having prefilled it.
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-followup')
    }, theme)
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  /** Push the card exactly as api_chat_slot_followup broadcasts it. */
  async function pushCard(items) {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({
      type: 'followup_card',
      data: { slot: SLOT, items, ts: Date.now() / 1000 },
    }))
    await page.waitForTimeout(900)
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Tight crop on the card + composer band, which is the whole story. */
  async function band(name) {
    const card = page.getByRole('group', { name: 'Follow-up suggestions' })
    if (await card.count()) {
      const box = await card.first().boundingBox()
      if (box) {
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x: Math.max(0, box.x - 24),
            y: Math.max(0, box.y - 16),
            width: Math.min(1500 - Math.max(0, box.x - 24), box.width + 48),
            height: box.height + 130,
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    await shot(name)
  }

  // 1. A single suggestion — the common case, all three actions visible.
  await load('dark')
  await pushCard([ITEMS[0]])
  await shot('01-single-dark')
  await band('02-single-dark-crop')

  // 2. Three suggestions stacked, most valuable first.
  await pushCard(ITEMS)
  await shot('03-three-dark')
  await band('04-three-dark-crop')

  // 3. "Add to this session" pre-fills the composer and does NOT send —
  //    the card closes, the prompt is sitting in the input awaiting send.
  await page.getByRole('button', { name: /Add to this session/ }).first().click()
  await page.waitForTimeout(1200)
  await shot('05-prefilled-composer-dark')

  // 4. A failed worktree create surfaces inline on the offending row instead of
  //    throwing, and leaves the button usable for a retry.
  scene.worktreeError = 'Branch already exists: feat/ws-rate-limit'
  await load('dark')
  await pushCard([ITEMS[0]])
  await page.getByRole('button', { name: /Start in new worktree/ }).first().click()
  await page.waitForTimeout(1500)
  await band('06-worktree-error-dark-crop')

  // 5. Light-theme parity.
  scene.worktreeError = null
  await load('light')
  await pushCard(ITEMS)
  await shot('07-three-light')
  await band('08-three-light-crop')

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
