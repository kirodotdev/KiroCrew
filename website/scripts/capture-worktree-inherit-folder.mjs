/**
 * Screenshot harness for "Start in new worktree" inheriting the spawning
 * session's sidebar folder (#6347).
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call and the /api/ws websocket intercepted by Playwright and answered
 * from fixtures. No gateway, no dashboard token, no git, no worktrees created.
 *
 * The client code under test is unmodified — only the network is stubbed — so the
 * follow-up card, the worktree action, and the sidebar folder grouping are
 * exercised exactly as they run in production. The card is driven the way the
 * backend drives it: by pushing a `followup_card` frame into the live websocket
 * after the page has rendered.
 *
 * The story in one image: the spawning session ("Add rate limiting to uploads")
 * is filed under the "Kiro Crew" project folder. Clicking "Start in new worktree"
 * creates the sibling worktree and opens a new session. With the fix, the create
 * call carries the spawning session's folder_id, so the server returns the new
 * slot ALREADY filed under "Kiro Crew" and the sidebar renders it nested there
 * rather than at the top level.
 *
 * Usage: node scripts/capture-worktree-inherit-folder.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6803'
const OUT = process.argv[3] || '../temp-screenshots/6347-worktree-inherit-folder'
const SLOT = 'chat-followup'
const WT_SLOT = 'chat-worktree'
const PROJECT = '/home/user/workspace/myproject'
const FOLDER_ID = 'folder-kirocrew'
const FOLDER_NAME = 'Kiro Crew'

mkdirSync(OUT, { recursive: true })

// The spawning session, filed under the Kiro Crew project folder.
const originSlot = {
  key: SLOT,
  title: 'Add rate limiting to uploads',
  running: false,
  last_message: 'Added the token-bucket limiter and its tests.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: FOLDER_ID,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}

// The worktree session the button opens. Created WITH the inherited folder_id,
// which is why it lands under the same folder. Added to the slots list only
// after the create call, matching how the real create broadcast works.
const worktreeSlot = {
  key: WT_SLOT,
  title: 'feat/ws-rate-limit',
  running: false,
  last_message: '',
  messages: 0,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT + '-wt-ws-rate-limit',
  folder_id: FOLDER_ID,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Add a rate limiter to the upload endpoint.' },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 30,
      content: 'Added a token-bucket limiter to `POST /api/upload` plus tests. All gates green.',
    },
  ],
}

const ITEM = {
  title: 'Add rate limiting to the WebSocket upgrade path',
  description:
    'The upload endpoint is bounded now, but /api/ws still accepts unlimited concurrent upgrades from one caller.',
  prompt:
    'Apply the same token-bucket limiter to the WebSocket upgrade path in src/kiro_crew/dashboard/ws.py, reusing the helper, and add tests for the reject path.',
  branch: 'feat/ws-rate-limit',
}

const folders = [{ id: FOLDER_ID, name: FOLDER_NAME, parent_id: null }]

// Flipped to true after the worktree create call, so the slots list starts with
// only the origin session and gains the worktree session exactly as it would
// after the create broadcast.
const scene = { worktreeCreated: false, theme: 'dark' }

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/worktree/create') {
      scene.worktreeCreated = true
      return json(route, {
        ok: true,
        path: PROJECT + '-wt-ws-rate-limit',
        branch: 'feat/ws-rate-limit',
        base: 'origin/HEAD',
      })
    }
    // The create round-trip goes through POST /api/chat/slots; return the
    // worktree slot echoing the folder_id the client sent (the behaviour the
    // fix produces). GET returns the list, growing once the worktree exists.
    if (path === '/api/chat/slots' && route.request().method() === 'POST') {
      return json(route, worktreeSlot)
    }
    if (path === '/api/chat/slots') {
      return json(route, scene.worktreeCreated ? [originSlot, worktreeSlot] : [originSlot])
    }
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path === '/api/chat/folders') return json(route, folders)
    if (path === '/api/kiro-prerequisite') {
      // Past the "Set up Kiro" gate: CLI installed, authenticated, ready.
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, repair_required: false, docs_url: '',
        login_command: 'kiro-cli login', sso_login_command: 'kiro-cli login', setup_allowed: true,
      })
    }
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
    scene.theme = theme
    scene.worktreeCreated = false
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-import-onboarded', '1')
      localStorage.setItem('mc-privacy-acked', '1')
      localStorage.setItem('mc-active-slot', 'chat-followup')
    }, theme)
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  async function pushCard(items) {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({ type: 'followup_card', data: { slot: SLOT, items, ts: Date.now() / 1000 } }))
    await page.waitForTimeout(900)
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  for (const theme of ['dark', 'light']) {
    await load(theme)
    // 1. Before: the spawning session sits under the Kiro Crew folder.
    await shot(`01-origin-filed-${theme}`)
    // 2. Trigger the worktree action from the follow-up card.
    await pushCard([ITEM])
    await page.getByRole('button', { name: /Start in new worktree/ }).first().click()
    await page.waitForTimeout(1800)
    // 3. After: the new worktree session is nested under the SAME folder,
    //    not orphaned at the sidebar's top level.
    await shot(`02-worktree-session-inherits-folder-${theme}`)
  }

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
