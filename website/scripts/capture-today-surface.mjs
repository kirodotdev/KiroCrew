/**
 * Screenshot harness for the Today mission-control surface.
 *
 * Runs the SPA against a dev/preview server with every /api/** call intercepted
 * by Playwright and answered from fixtures. No gateway, no dashboard token, no
 * sessions created.
 *
 * The seeded slots deliberately populate all three buckets so the shot proves
 * the real feature, not an empty state:
 *   - Needs You: one owed tool approval (with tool + input for the drawer) and
 *     one blocked question (needs_input) — the two ways a slot demands a human.
 *   - Working: a running turn and a slot with sub-agents in flight.
 *   - Completed: two finished turns.
 *
 * Usage: node scripts/capture-today-surface.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6807'
const OUT = process.argv[3] || '../temp-screenshots/today-surface'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const iso = (secsAgo) => new Date((now - secsAgo) * 1000).toISOString()

const slots = [
  {
    key: 'chat-approval',
    title: 'Deploy the staging stack',
    running: true,
    pending_approval: true,
    pending_approval_info: {
      request_id: 'req-1',
      tool: 'execute_bash',
      tool_input: 'terraform apply -auto-approve staging.tfplan',
    },
    last_message: 'Ready to apply the staging Terraform plan.',
    agent: 'kirocrew',
    last_activity_ts: iso(30),
  },
  {
    key: 'chat-question',
    title: 'Pick the cache eviction policy',
    running: false,
    needs_input: true,
    needs_input_reason: 'question',
    last_message: 'Both LRU and LFU fit the read pattern — which do you want?',
    agent: 'kirocrew',
    slack_linked: true,
    slack_channel: '#eng-cache',
    last_activity_ts: iso(120),
  },
  {
    key: 'chat-running',
    title: 'Add the token-bucket rate limiter',
    running: true,
    last_message: 'Writing the limiter and its unit tests.',
    agent: 'kirocrew',
    last_activity_ts: iso(15),
  },
  {
    key: 'chat-subagents',
    title: 'Audit the dependency tree for CVEs',
    running: true,
    subagents_running: true,
    last_message: 'Three sub-agents scanning workspaces in parallel.',
    agent: 'kirocrew',
    last_activity_ts: iso(45),
  },
  {
    key: 'chat-done-1',
    title: 'Migrate the settings panel to Radix',
    running: false,
    last_message: 'Migrated and all gates green.',
    agent: 'kirocrew',
    last_activity_ts: iso(900),
  },
  {
    key: 'chat-done-2',
    title: 'Fix the flaky Escape-to-close test',
    running: false,
    last_message: 'Root-caused the focus leak; test is stable now.',
    agent: 'kirocrew',
    last_activity_ts: iso(1800),
  },
]

const scene = { theme: 'dark' }

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  // Exact-path fixtures. `theme` is read live from `scene` so the same map
  // serves both the dark and light passes. The first-run gate
  // (`/api/kiro-prerequisite`) MUST report ready, else the capture is a
  // screenshot of "Set up Kiro" rather than the Today surface.
  const fixtures = () => ({
    '/api/chat/slots': slots,
    '/api/approvals': [],
    '/api/kiro-prerequisite': { platform: 'linux', installed: true, authenticated: true, ready: true, initial_setup_complete: true, repair_required: false, setup_allowed: true },
    '/api/status': { sessions: 6, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
    '/api/notifications': { notifications: [], unread: 0 },
    '/api/auth/me': { user: 'owner', app: '' },
    '/api/models': { models: [], default: 'auto' },
    '/api/themes': { themes: [], installed: [] },
    '/api/theme/boot': { mode: scene.theme, theme: '' },
    '/api/dashboard/branding': { bot_name: 'Kiro', avatar: '' },
    '/api/chat/nav/resolve-links': { summaries: [] },
  })

  // Match ONLY real backend calls whose pathname starts with `/api/`. A loose
  // `**/api/**` glob also matches dev-served source modules like
  // `/src/api/queryClient.ts`, answering them with JSON and breaking the module
  // graph (the built-dist harnesses avoid this only because sources are bundled
  // into hashed chunks). Everything else falls through to Vite.
  await page.route('**', async route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    const table = fixtures()
    if (path in table) return json(route, table[path])
    if (path.startsWith('/api/chat/slots/')) return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] })
    // Anything scalar-shaped gets an empty object; list endpoints get an array.
    return json(route, /(config|tips|voice|autonudge|usage)/.test(path) ? {} : [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  async function load(theme) {
    scene.theme = theme
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-privacy-acked', '1')
    }, theme)
    await page.goto(BASE + '/today', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3000)
  }

  await load('dark')
  await page.screenshot({ path: `${OUT}/01-today-dark.png` })
  console.log('wrote', `${OUT}/01-today-dark.png`)

  await load('light')
  await page.screenshot({ path: `${OUT}/02-today-light.png` })
  console.log('wrote', `${OUT}/02-today-light.png`)

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
