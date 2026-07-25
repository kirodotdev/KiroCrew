/**
 * Screenshot harness for the pull-request state-sync change.
 *
 * Runs the REAL built SPA (website/dist) against a static file server, with
 * every /api/** call and the /api/ws websocket intercepted by Playwright and
 * answered from fixtures. No gateway, no dashboard token, no provider calls.
 *
 * The client code under test is unmodified: only the network is stubbed, so the
 * chips, the Changes strip, and the delta handling are exercised exactly as they
 * run in production.
 *
 * Usage: node scripts/capture-pr-state-sync.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6801'
const OUT = process.argv[3] || '../docs/screenshots/pr-state-sync'
const SLOT = 'chat-pr-state'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/443'
const OTHER_URL = 'https://github.com/kirodotdev/KiroCrew/pull/409'

mkdirSync(OUT, { recursive: true })

/** Slot payload. `chipState`/`chipCi` are what the SIDEBAR renders. */
const slots = (chipState, chipCi) => [{
  key: SLOT,
  title: 'Investigate Scrolling Bug Long Sessions',
  running: false,
  last_message: 'Conflict resolved. PR #443 is MERGEABLE again…',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: Math.floor(Date.now() / 1000),
  source_links: [
    { provider: 'github', number: 443, url: PR_URL, state: chipState, ci: chipCi },
    { provider: 'github', number: 409, url: OTHER_URL, state: 'open', ci: 'running' },
  ],
  source_links_total: 2,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', content: 'Fix the pull-request state sync', ts: Date.now() / 1000 - 600 },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 60,
      content: `Opened ${PR_URL} and ${OTHER_URL} for the follow-up.`,
    },
  ],
}

/** Full payload the DETAIL PANEL renders. */
const source = (state, mergedAt) => ({
  provider: 'github',
  url: PR_URL,
  number: 443,
  title: 'fix(chat): keep pull-request state in sync across sidebar and detail panel',
  description: 'Unify the status caches, refresh at turn boundaries, push deltas.',
  state,
  draft: false,
  mergedAt,
  updatedAt: new Date().toISOString(),
  headBranch: 'fix/pr-state-sync',
  baseBranch: 'main',
  headSha: 'd4c42602',
  author: 'kyleseaman',
  additions: 824,
  deletions: 7,
  changedFiles: 11,
  mergeable: 'mergeable',
  mergeStateStatus: state === 'MERGED' ? 'clean' : 'blocked',
  commits: [{
    sha: 'd4c42602', message: 'fix(chat): keep pull-request state in sync',
    author: 'kyleseaman', committedAt: new Date().toISOString(), url: PR_URL,
  }],
  checks: [
    { name: 'Backend Tests', workflow: 'CI', status: 'COMPLETED', conclusion: 'SUCCESS', bucket: 'passed', url: '', startedAt: '', completedAt: '' },
    { name: 'Frontend Tests', workflow: 'CI', status: 'COMPLETED', conclusion: 'SUCCESS', bucket: 'passed', url: '', startedAt: '', completedAt: '' },
    { name: 'mypy', workflow: 'CI', status: 'COMPLETED', conclusion: 'SUCCESS', bucket: 'passed', url: '', startedAt: '', completedAt: '' },
  ],
  comments: [],
  files: [
    { path: 'src/kiro_crew/dashboard/handlers/source_providers.py', status: 'modified', additions: 151, deletions: 4, patch: '' },
    { path: 'src/kiro_crew/dashboard/state.py', status: 'modified', additions: 46, deletions: 0, patch: '' },
    { path: 'website/src/hooks/useWebSocket.ts', status: 'modified', additions: 32, deletions: 1, patch: '' },
    { path: 'website/src/utils/pullRequestStatusDelta.ts', status: 'added', additions: 62, deletions: 0, patch: '' },
  ],
})

/** Mutable scenario state the route handlers read. */
const scene = {
  chipState: 'open',
  chipCi: 'passed',
  sourceState: 'MERGED',
  sourceMergedAt: new Date().toISOString(),
  statuses: { [PR_URL]: { state: 'open', ci: 'passed' }, [OTHER_URL]: { state: 'open', ci: 'running' } },
}

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    // The whole story is a 10px chip glyph next to a panel badge, so capture at
    // 2x — a 1x full-page shot renders it illegible on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  // Keep the socket open and silent; we push frames into it by hand later.
  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/chat/slots') return json(route, slots(scene.chipState, scene.chipCi))
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path === '/api/source/pull-request') return json(route, source(scene.sourceState, scene.sourceMergedAt))
    if (path === '/api/source/pull-request/status') {
      return json(route, { statuses: scene.statuses, refreshing: [], ttlSecs: 60 })
    }
    if (path === '/api/source/pull-request/checks') return json(route, { checks: source(scene.sourceState, '').checks })
    if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/config' || path === '/api/kirocrew-config') return json(route, {})
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    // Object-shaped singletons; everything else the dashboard polls is a list.
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 200))
  })

  async function load(theme) {
    await page.addInitScript(t => {
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-pr-state')
    }, theme)
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    // Open the Changes view so the sidebar chip and the detail payload are on
    // camera together — the whole point is whether they agree.
    const opener = page.getByRole('button', { name: 'Open activity panel' })
    if (await opener.count()) {
      await opener.first().click().catch(() => {})
      await page.waitForTimeout(1200)
    }
    const changes = page.getByRole('button', { name: /^Changes/ })
    if (await changes.count()) {
      await changes.first().click().catch(() => {})
      await page.waitForTimeout(2000)
    }
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Wide crop holding the sidebar chip row AND the panel header in one frame. */
  async function strip(name) {
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: 240, y: 100, width: 1360, height: 220 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // 1. The bug: chip still says open + CI green while the panel already knows
  //    the PR merged. Two caches, two answers, same PR.
  await load('dark')
  await shot('01-desync-before-dark')
  await strip('01b-desync-before-crop')

  // 2. After the write-through: the chip carries the merge glyph the panel shows.
  scene.chipState = 'merged'
  scene.statuses[PR_URL] = { state: 'merged', ci: 'passed' }
  await load('dark')
  await shot('02-synced-after-dark')
  await strip('02b-synced-after-crop')

  // 3. Live delta: with the page already rendered, push a source_status frame
  //    for the OTHER pull request and let the strip update with no poll.
  if (wsServer) {
    await page.waitForTimeout(500)
    await strip('03a-delta-before-crop')
    wsServer.send(JSON.stringify({
      type: 'source_status',
      data: { url: OTHER_URL, origin: 'chip', state: 'merged', ci: 'passed' },
    }))
    await page.waitForTimeout(1500)
    await strip('03b-delta-after-crop')
  }

  // 4. Light theme parity for the synced state.
  await load('light')
  await shot('04-synced-after-light')
  await strip('04b-synced-after-light-crop')

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
