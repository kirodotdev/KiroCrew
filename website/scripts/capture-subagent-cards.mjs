/**
 * Screenshot harness for the subagent activity-card header layout.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call and the /api/ws websocket intercepted by Playwright and answered
 * from fixtures. No gateway, no dashboard token, no subagents actually spawned.
 *
 * The client code under test is unmodified — only the network is stubbed — so the
 * activity rail, its cards and their headers are laid out exactly as they are in
 * production. Cards are driven the way the backend drives them: by pushing
 * `subagent_snapshot` / `subagent_tool` / `subagent_done` frames into the live
 * websocket after the page has rendered.
 *
 * The rail width is the whole point of this harness (the header wrapped only in a
 * narrow rail), so each scenario is captured at several persisted widths via the
 * panel's own `mc-side-panel-width` key.
 *
 * Usage: node scripts/capture-subagent-cards.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6803'
const OUT = process.argv[3] || '../temp-screenshots/subagent-card-header'
const SLOT = 'chat-subagents'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Fix the subagent card header',
  running: true,
  last_message: 'Spawned 3 agents, waiting for results…',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 700, content: 'Research the activity rail layout.' },
    { role: 'assistant', ts: Date.now() / 1000 - 690, content: 'Spawned 3 agents, waiting for results…' },
  ],
}

const now = () => Date.now() / 1000

/** Three cards: a long-running tool call, a second worker, and a finished one
 *  (the finished card is what surfaces the "Dismiss done" batch control). */
const CARDS = [
  {
    id: 'sa-research',
    agent: 'kirocrew',
    task: 'READ-ONLY RESEARCH (do not modify any file): map how the activity rail lays out subagent cards and report the header structure.',
    last_tool: 'read /website/src/pages/chat/ActivityViewer.tsx',
    started: now() - 239,
    streaming: 'Reading ActivityViewer.tsx…\nHeader is a flex row: status icon, title, agent chip, elapsed, Cancel.\n',
    tool_count: 14,
  },
  {
    id: 'sa-review',
    // A longer agent name proves the chip is capped instead of starving the
    // clock and the Cancel button.
    agent: 'kirocrew-reviewer',
    task: 'Mirror the GPT review gate over the working diff and report Critical/High findings only.',
    last_tool: 'grep shrink-0',
    started: now() - 74,
    streaming: 'Scanning the diff for reachable defects…\n',
    tool_count: 6,
  },
]

const DONE_CARD = {
  id: 'sa-gates',
  agent: 'kirocrew',
  task: 'Run the frontend gates (tsc -b, eslint, vitest) and report failures.',
  last_tool: 'execute_bash npx vitest run',
  started: now() - 421,
  streaming: '',
  tool_count: 22,
}

const FIXED_API = new Map([
  ['/api/status', { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' }],
  ['/api/notifications', { notifications: [], unread: 0 }],
  ['/api/auth/me', { user: 'owner', app: '' }],
  ['/api/models', { models: [], default: 'auto' }],
  ['/api/themes', { themes: [], installed: [] }],
  ['/api/dashboard/branding', { bot_name: 'Kiro', avatar: '' }],
  ['/api/recent-projects', { dirs: [PROJECT] }],
  ['/api/chat/nav/resolve-links', { summaries: [] }],
])

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The header is dense small type (11–13px); a 1x shot renders it soft.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  const scene = { theme: 'dark' }

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // The setup gate runs BEFORE the app shell: an array-shaped stub makes it
    // read `.operation.status` off undefined and the whole shell error-boundaries
    // out with nothing rendered.
    if (path.startsWith('/api/kiro-prerequisite')) {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: true,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    // The app shell iterates this on boot; an object-shaped stub throws inside
    // the ErrorBoundary and nothing renders at all.
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    // These boot fixtures are static across every scene; keeping them in a map
    // makes this harness's route contract explicit without cloning the long
    // condition chain used by other screenshot harnesses.
    if (FIXED_API.has(path)) return json(route, FIXED_API.get(path))
    if (path === '/api/theme/boot') return json(route, { mode: scene.theme, theme: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  /** Cold-load the SPA with the rail CLOSED at the persisted `railWidth`; the
   *  Subagents view is opened afterwards the way a user opens it (clicking a
   *  row of the in-chat subagent progress bar), because the panel's tab strip
   *  only carries views that were actually opened. */
  async function load(railWidth, theme = 'dark') {
    scene.theme = theme
    await page.addInitScript(([t, slot, w]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      // ChatPage persists the active slot per mode.
      localStorage.setItem('mc-active-slot-chat', slot)
      // The panel persists its own width — seeding it reproduces a user's
      // narrow rail without dragging the splitter.
      localStorage.setItem('mc-side-panel-width', String(w))
    }, [theme, SLOT, railWidth])
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  const send = async (type, data, settle = 400) => {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({ type, data }))
    await page.waitForTimeout(settle)
  }

  /** Replay the cards exactly as the gateway streams them. */
  async function pushCards() {
    for (const c of [...CARDS, DONE_CARD]) {
      await send('subagent_snapshot', { ...c, slot: SLOT }, 150)
    }
    for (const c of CARDS) {
      await send('subagent_tool', { slot: SLOT, id: c.id, tool: c.last_tool, tool_count: c.tool_count }, 150)
    }
    await send('subagent_done', {
      slot: SLOT, id: DONE_CARD.id, elapsed: 421, outcome: 'completed',
    }, 600)
    // Open the Subagents view the way a user does: the in-chat progress bar row
    // dispatches openActivityToTab('subagents'), which opens the panel on that
    // view. Done cards auto-collapse 2s after they land — let that settle.
    await page.getByTestId('subagent-row').first().getByRole('button').first().click()
    await page.waitForTimeout(2600)
  }

  async function shot(name, clipToRail = true) {
    const panelBox = await page.evaluate(() => {
      const region = document.querySelector('[role="region"][aria-label="Activity"]')
      if (!region) return null
      const panel = region.parentElement || region
      const r = panel.getBoundingClientRect()
      return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }
    })
    if (clipToRail && panelBox) {
      await page.screenshot({ path: `${OUT}/${name}.png`, clip: panelBox })
    } else {
      await page.screenshot({ path: `${OUT}/${name}.png` })
    }
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // Rail widths: the panel's default (460), a comfortably resized rail (400),
  // and the panel's own minimum (320) — the width the header broke at.
  for (const w of [460, 400, 320]) {
    await load(w)
    await pushCards()
    await shot(`rail-${w}-dark`)
    if (w === 400) {
      await load(w, 'light')
      await pushCards()
      await shot(`rail-${w}-light`)
    }
    if (w === 460) await shot('full-window-dark', false)
  }

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
