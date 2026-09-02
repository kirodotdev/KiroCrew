/**
 * Screenshot harness for the owner-not-configured mutation guidance.
 *
 * Runs the REAL built SPA (website/dist) against the shared static server,
 * with every /api/** call intercepted by Playwright and answered from
 * fixtures. No gateway, no dashboard token, no provider calls.
 *
 * Scene: a standalone local install (no configured owner) loads a pull
 * request in the Changes panel — reads pass — and the user clicks
 * Enable auto-merge, then Confirm. The mutation is refused with the coded
 * 403 this change introduces, and the panel must render the localized
 * guidance (what to configure, where) plus the Slack-settings link instead
 * of a bare "forbidden".
 *
 * Usage: node scripts/capture-owner-not-configured.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/owner-not-configured-hint'
const PREFIX = process.argv[3] || 'after'
if (!/^[A-Za-z0-9._-]+$/.test(PREFIX)) {
  console.error(`prefix must match [A-Za-z0-9._-]+, got: ${PREFIX}`)
  process.exit(2)
}
const SLOT = 'chat-owner-hint'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/6420'
// Keep every relative-time label stable so a visual diff only reflects product
// changes, not the wall clock of the machine taking the screenshot.
const FIXTURE_NOW_SECONDS = 1_750_000_000

mkdirSync(OUT, { recursive: true })

const slots = () => [{
  key: SLOT,
  title: 'Owner-not-configured guidance',
  running: false,
  last_message: 'Opened the PR in the Changes panel…',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: FIXTURE_NOW_SECONDS,
  source_links: [
    { provider: 'github', number: 6420, url: PR_URL, state: 'open', ci: 'passed' },
  ],
  source_links_total: 1,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', content: 'Open the PR', ts: FIXTURE_NOW_SECONDS - 600 },
    { role: 'assistant', ts: FIXTURE_NOW_SECONDS - 60, content: `Working on ${PR_URL}.` },
  ],
}

const source = {
  provider: 'github',
  url: PR_URL,
  number: 6420,
  title: 'fix(dashboard): name the remedy when a provider mutation needs a configured owner',
  description: 'Owner-gated mutations on a local install now explain what to configure.',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: '2025-06-15T15:06:40.000Z',
  headBranch: 'fix/owner-not-configured-hint',
  baseBranch: 'main',
  headSha: 'abc1234',
  author: 'kirocrew',
  additions: 40,
  deletions: 4,
  changedFiles: 4,
  mergeable: 'mergeable',
  mergeStateStatus: 'clean',
  autoMerge: false,
  commits: [{
    sha: 'abc1234', message: 'fix(dashboard): owner-not-configured guidance',
    author: 'kirocrew', committedAt: '2025-06-15T15:06:40.000Z', url: PR_URL,
  }],
  checks: [{ name: 'ci', bucket: 'success', status: 'completed', conclusion: 'success', url: '' }],
  comments: [],
  files: [
    { path: 'src/kiro_crew/dashboard/handlers/source_providers.py', status: 'modified', additions: 20, deletions: 2, patch: '' },
  ],
  partialSections: [],
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

const FIXED_ROUTES = {
  '/api/kiro-prerequisite': { ready: true },
  '/api/status': { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/config': {},
  '/api/kirocrew-config': {},
  '/api/dashboard/branding': { bot_name: 'Kiro', avatar: '' },
  '/api/auth/me': { user: 'local-app', app: '' },
  '/api/models': { models: [], default: 'auto' },
  '/api/themes': { themes: [], installed: [] },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/chat/nav/resolve-links': { summaries: [] },
}

async function main() {
  const { srv: server, base } = await serveDist()
  const browser = await chromium.launch()
  // Framer Motion runs on animation frames rather than the CSS animation API,
  // so screenshot({ animations: 'disabled' }) alone cannot freeze every shell
  // transition. Ask the real app to take its reduced-motion path from startup.
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    deviceScaleFactor: 2,
    reducedMotion: 'reduce',
  })
  const page = await context.newPage()

  // The production shell preloads Space Grotesk from Google Fonts. Whether that
  // stylesheet wins the first paint depends on network/cache timing, which made
  // two otherwise identical captures wrap the guidance at different words.
  // Evidence should not depend on an external font CDN: exercise the product's
  // built-in System preference and refuse both font origins explicitly.
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  await page.route('https://fonts.gstatic.com/**', route => route.abort())
  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/source/pull-request/auto-merge') {
      // The refusal under test: a signed local session with no configured
      // owner gets the coded, actionable 403 instead of a bare forbidden.
      return json(route, {
        error: 'this action needs a configured owner, which Kiro Crew identifies by Slack member ID',
        code: 'owner_not_configured',
      }, 403)
    }
    if (path in FIXED_ROUTES) return json(route, FIXED_ROUTES[path])
    if (path === '/api/chat/slots') return json(route, slots())
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path === '/api/source/pull-request') return json(route, source)
    if (path === '/api/source/pull-request/status') {
      return json(route, { statuses: { [PR_URL]: { state: 'open', ci: 'passed' } }, refreshing: [], ttlSecs: 60 })
    }
    if (path === '/api/source/pull-request/checks') return json(route, { checks: source.checks })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })

  const pageErrors = []
  page.on('pageerror', err => pageErrors.push(String(err)))

  await page.addInitScript(({ fixtureNowSeconds }) => {
    Date.now = () => fixtureNowSeconds * 1000
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-font-family', 'system')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', 'chat-owner-hint')
  }, { fixtureNowSeconds: FIXTURE_NOW_SECONDS })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  // The sidebar and its active-slot hydration are independent of the shell.
  // Select the fixture only after its card exists so opening the activity panel
  // cannot race a still-empty source-link projection.
  const slotTitle = page.getByText('Owner-not-configured guidance', { exact: true }).first()
  await slotTitle.waitFor({ state: 'visible' })
  await slotTitle.click()
  const opener = page.getByRole('button', { name: 'Open activity panel' })
  await opener.first().waitFor({ state: 'visible' })
  await opener.first().click()
  // SidePanel's pinned views use the ARIA tab contract on current main.
  const changes = page.getByRole('tab', { name: /^Changes/ })
  await changes.first().waitFor({ state: 'visible' })
  await changes.first().click()

  // Drive the two-click auto-merge flow into the refusal.
  const enableAutoMerge = page.getByRole('button', { name: /enable auto-merge/i }).first()
  await enableAutoMerge.waitFor({ state: 'visible' })
  await enableAutoMerge.click()
  const confirmAutoMerge = page.getByRole('button', { name: /confirm auto-merge/i }).first()
  await confirmAutoMerge.waitFor({ state: 'visible' })
  await Promise.all([
    page.waitForResponse(response => (
      new URL(response.url()).pathname === '/api/source/pull-request/auto-merge'
      && response.status() === 403
    )),
    confirmAutoMerge.click(),
  ])

  const link = page.getByRole('link', { name: /slack settings/i }).first()
  await link.waitFor({ state: 'visible' })
  await page.evaluate(async () => { await document.fonts.ready })
  if (pageErrors.length) throw new Error(`page errors: ${pageErrors.join('; ')}`)

  await page.screenshot({ path: `${OUT}/${PREFIX}-guidance-full.png`, animations: 'disabled' })
  console.log('wrote', `${OUT}/${PREFIX}-guidance-full.png`)

  // Crop around the action row so the guidance and the settings link read
  // at review size.
  const box = await link.boundingBox()
  if (!box) throw new Error('Slack settings link has no bounding box')
  const x = Math.max(0, box.x - 620)
  const clip = { x, y: Math.max(0, box.y - 170), width: 1600 - x, height: box.height + 240 }
  await page.screenshot({ path: `${OUT}/${PREFIX}-guidance-crop.png`, clip, animations: 'disabled' })
  console.log('wrote', `${OUT}/${PREFIX}-guidance-crop.png`)

  await browser.close()
  server.close()
}

main().catch(err => { console.error(err); process.exit(1) })
