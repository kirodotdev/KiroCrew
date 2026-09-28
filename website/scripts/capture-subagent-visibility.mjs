/**
 * Screenshot harness for sub-agent spawn visibility.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call and the /api/ws websocket intercepted by Playwright and answered
 * from fixtures. No gateway, no dashboard token, no subagents actually spawned.
 *
 * The client code under test is unmodified — only the network is stubbed — so the
 * inline card, the queued banner, the sidebar subtitle and the nav-rail dot are
 * exercised exactly as they run in production. State is driven the way the
 * gateway drives it: by pushing `subagent_queued` / `subagent_spawn` /
 * `subagent_tool` / `subagent_done` frames into the live websocket after the page
 * has rendered (see _subagent_event in slack/gateway.py).
 *
 * Usage: node scripts/capture-subagent-visibility.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6803'
const OUT = process.argv[3] || '../temp-screenshots/subagent-visibility'
const SLOT = 'chat-subagents'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** The three agents the wave launches. Ids mirror SubagentManager's hex digests. */
const AGENTS = [
  { id: '1713e7d0', task: 'Trace the backend spawn signals — what does the gateway broadcast?' },
  { id: '5c15adde', task: 'Trace the sidebar and panel data flow — where does the count come from?' },
  { id: 'aa5da49b', task: 'Trace the chat transcript — which tool calls get a dedicated card?' },
]

/** Exactly the text mcp_core.py's spawn_run handler returns, which is what the
 *  card's detector parses out of the persisted tool output. */
const SPAWN_OUTPUT = [
  'Spawned 3 subagent(s). Results will arrive as completion events:',
  ...AGENTS.map(a => `  ${a.id} (kirocrew): ${a.task}`),
  '',
  '⚠️ END YOUR TURN NOW — do no further work this turn.'
    + ' Wait for the [Subagent completion event] messages, which will resume you.',
].join('\n')

const slots = [
  {
    key: SLOT,
    title: 'Sub-agent spawning visibility',
    running: false,
    last_message: 'Spawned 3 investigation agents. Waiting for results.',
    messages: 4,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
  {
    key: 'chat-idle',
    title: 'Rename getUserName to getUsername',
    running: false,
    last_message: 'Renamed across 4 files.',
    messages: 6,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000) - 3600,
    source_links: [],
    source_links_total: 0,
  },
]

const now = Date.now() / 1000
const detail = {
  running: false,
  has_more: false,
  total: 4,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: now - 240,
      content: 'Two different sessions just spawned sub agents — why is there no indication?',
    },
    {
      role: 'tool',
      ts: now - 200,
      content: '🔧 spawn_run',
      meta: {
        tool_call_id: 'tc_spawn_1',
        purpose: 'Fan the investigation out across three independent legs',
        input: JSON.stringify({ tasks: AGENTS.map(a => a.task) }),
        output: SPAWN_OUTPUT,
      },
    },
    {
      role: 'assistant',
      ts: now - 190,
      content:
        'Spawned 3 investigation agents (backend spawn signals, sidebar/panel data flow, '
        + 'chat transcript card rendering). Waiting for results before writing any code.',
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
    // The card and the rail dot are dense small type (10–13px); a 1x shot
    // renders them soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // Reconcile loop in SubagentProgressBar: report the wave as still live so a
    // 30s tick can't sweep the agents out mid-capture.
    if (path === '/api/spawn') {
      return json(route, {
        agents: AGENTS.map(a => ({
          id: a.id, task: a.task, done: false, parent: `dashboard:${SLOT}`, agent: 'kirocrew',
        })),
      })
    }
    // The app shell iterates this on boot; an object-shaped stub throws inside
    // the ErrorBoundary and nothing renders at all.
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') return json(route, { sessions: 2, crons: 0, lessons: 0, uptime: 900, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: scene.theme, theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    // KiroPrerequisiteGate wraps the whole app shell and reads
    // `status.operation.status` (optional-chained only at `status`), so a bare
    // {} stub throws inside the ErrorBoundary and nothing renders.
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        ready: true,
        setup_allowed: true,
        operation: { status: 'idle', message: '' },
      })
    }
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
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-subagents')
    }, theme)
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  const push = async (type, data, settle = 700) => {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({ type, data }))
    await page.waitForTimeout(settle)
  }

  /** The wave is accepted but every member is still behind the concurrency cap.
   *  This is the state that used to read as "No subagents running". */
  const queueWave = () => push('subagent_queued', { slot: SLOT, queued: 3 })

  /** Staggered ramp: agents start one at a time, so running and queued coexist. */
  async function startAgent(i) {
    await push('subagent_queued', { slot: SLOT, queued: 3 - (i + 1) }, 150)
    await push('subagent_spawn', { slot: SLOT, id: AGENTS[i].id, task: AGENTS[i].task, agent: 'kirocrew' }, 150)
    await push('subagent_tool', { slot: SLOT, id: AGENTS[i].id, tool: 'fs_read', turns: 1, tool_count: 3 })
  }

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Tight crop around a locator, with padding, clamped to the viewport. */
  async function crop(name, locator, pad = { x: 24, y: 16, w: 48, h: 40 }) {
    const el = locator.first()
    if (await el.count()) {
      const box = await el.boundingBox()
      if (box) {
        const x = Math.max(0, box.x - pad.x)
        const y = Math.max(0, box.y - pad.y)
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x, y,
            width: Math.min(1500 - x, box.width + pad.w),
            height: Math.min(950 - y, box.height + pad.h),
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    console.log('WARN: locator not found for', name, '- full page instead')
    await shot(name)
  }

  const card = () => page.getByTestId('subagent-run-card')

  // 1. Queued wave: the inline card records the launch and says 3 are waiting,
  //    the sidebar row says "3 agents queued", the rail carries the dot.
  await load('dark')
  await queueWave()
  await shot('01-queued-dark')
  await crop('02-queued-card-crop', card())
  await crop('03-queued-sidebar-crop', page.getByText('3 agents queued'), { x: 12, y: 40, w: 320, h: 90 })

  // 2. Staggered ramp: one agent started, two still queued — the case the old
  //    running-only count reported as "1 agent running".
  await startAgent(0)
  await crop('04-ramp-card-crop', card())
  await crop('05-ramp-sidebar-crop', page.getByText('1 running · 2 queued'), { x: 12, y: 40, w: 320, h: 90 })

  // 3. Subagents panel, opened by clicking the card. The queued banner is the
  //    fix for the panel that used to read "No subagents running".
  await card().click()
  await page.waitForTimeout(1200)
  await shot('06-panel-queued-dark')
  await crop('07-panel-queued-banner-crop', page.getByTestId('subagent-queued-banner'), { x: 16, y: 16, w: 40, h: 200 })

  // 4. Whole wave running, then settled — the card stays in scrollback with the
  //    outcome instead of vanishing with the transient chip.
  await startAgent(1)
  await startAgent(2)
  await shot('08-running-dark')
  for (const a of AGENTS) {
    await push('subagent_done', { slot: SLOT, id: a.id, elapsed: 96, outcome: 'completed' }, 200)
  }
  await page.waitForTimeout(800)
  await crop('09-settled-card-crop', card())

  // 5. Nav rail: the cross-page signal. Expanded shows Bot + count; the dot is
  //    what a user on another page sees.
  await load('dark')
  await queueWave()
  // Fixed rect over the rail's top rows: NavItem renders a role="button"
  // Clickable whose accessible name is perturbed by the activity dot's own
  // role="status" child, so a locator-based crop is unreliable here.
  await page.screenshot({
    path: `${OUT}/10-nav-rail-crop.png`,
    clip: { x: 10, y: 60, width: 380, height: 200 },
  })
  console.log('wrote', `${OUT}/10-nav-rail-crop.png`)

  // 6. Light-theme parity for the two new surfaces.
  await load('light')
  await queueWave()
  await shot('11-queued-light')
  await crop('12-queued-card-light-crop', card())

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
