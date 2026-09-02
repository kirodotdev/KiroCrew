/**
 * Screenshot harness for the session "needs your answer" status.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call intercepted by Playwright and answered from fixtures. No gateway,
 * no dashboard token, no sessions created.
 *
 * The four sidebar rows are one deliberate comparison, top to bottom: an owed
 * tool approval (warn, outranks everything), an unanswered question card (info —
 * the one thing `needs_input` is raised for, shown even though the slot reports
 * running), a turn that ENDED offering `[OPTIONS:]` (not an ask: it keeps its
 * message and its dot), and a running session showing live turn status. Reading
 * them together is how the precedence and the "one marker per row" rule are
 * checked by eye.
 *
 * The two middle rows are seeded UNREAD (`mc-unread-slots`) because the dot is
 * half of what the scene proves: the card drops it, the finished `[OPTIONS:]`
 * turn keeps it.
 *
 * Usage: node scripts/capture-needs-input-status.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6802'
const OUT = process.argv[3] || '../temp-screenshots/needs-input-status'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const slots = [
  {
    key: 'chat-approval',
    title: 'Deploy the staging stack',
    running: true,
    pending_approval: true,
    last_message: 'Running terraform apply on the staging workspace.',
    messages: 12,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    modified: now,
    last_ts: new Date((now - 40) * 1000).toISOString(),
    source_links: [],
    source_links_total: 0,
  },
  {
    key: 'chat-question',
    title: 'Pick the cache eviction policy',
    running: true,
    needs_input: true,
    last_message: 'Both policies fit the read pattern.',
    messages: 8,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    modified: now,
    last_ts: new Date((now - 90) * 1000).toISOString(),
    source_links: [],
    source_links_total: 0,
  },
  {
    key: 'chat-options',
    title: 'Rebase the devcontainer branch',
    running: false,
    last_message: 'CI is green except the shelf button-count rule.',
    messages: 21,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    modified: now,
    last_ts: new Date((now - 240) * 1000).toISOString(),
    source_links: [],
    source_links_total: 0,
  },
  {
    key: 'chat-running',
    title: 'Add the token-bucket limiter',
    running: true,
    last_message: 'Reading the existing middleware.',
    messages: 6,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    modified: now,
    last_ts: new Date((now - 12) * 1000).toISOString(),
    source_links: [],
    source_links_total: 0,
  },
]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', ts: now - 600, content: 'Which eviction policy should the cache use?' },
    {
      role: 'assistant',
      ts: now - 90,
      content: 'Both fit the read pattern.\n\n[OPTIONS: Use LRU | Use LFU | Show me the numbers]',
    },
  ],
}

const scene = { theme: 'dark' }

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The rows are dense small type (11–13px); a 1x shot renders them soft.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // The first-run gate is a FULL-SCREEN takeover until `ready` — without this
    // the capture is a screenshot of "Set up Kiro", not of the sidebar.
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'linux',
        installed: true,
        authenticated: true,
        ready: true,
        initial_setup_complete: true,
        repair_required: false,
        docs_url: 'https://kiro.dev',
        login_command: 'kiro-cli login',
        sso_login_command: 'kiro-cli login --sso',
        setup_allowed: true,
      })
    }
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') return json(route, { sessions: 4, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: scene.theme, theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  async function load(theme) {
    scene.theme = theme
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      // The approval row is the active session, so the four rows read as one
      // list with a single focus rather than four competing highlights — and it
      // keeps the active highlight off the two rows the scene is about.
      localStorage.setItem('mc-active-slot', 'chat-approval')
      // Seeds `unreadSlots` (dashboardSlice reads this key at store init), which
      // is otherwise websocket-driven and therefore absent from a fixture run.
      localStorage.setItem('mc-unread-slots', JSON.stringify(['chat-question', 'chat-options']))
    }, theme)
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  /** Crop to the sidebar's session list, which is the whole story. */
  async function sidebar(name) {
    const row = page.locator('[data-slot-key="chat-question"]').first()
    if (await row.count()) {
      const box = await row.boundingBox()
      if (box) {
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x: 0,
            y: Math.max(0, box.y - 130),
            width: Math.min(1500, box.x + box.width + 40),
            height: 420,
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote (full page fallback)', `${OUT}/${name}.png`)
  }

  await load('dark')
  await page.screenshot({ path: `${OUT}/01-dashboard-dark.png` })
  console.log('wrote', `${OUT}/01-dashboard-dark.png`)
  await sidebar('02-sidebar-dark')

  await load('light')
  await sidebar('03-sidebar-light')

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
