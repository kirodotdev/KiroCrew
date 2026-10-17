/**
 * Screenshot harness for the selected-PR CI glyph on degraded payloads.
 *
 * Runs the REAL built SPA (website/dist) against a static file server, with
 * every /api/** call intercepted by Playwright and answered from fixtures.
 * No gateway, no dashboard token, no provider calls.
 *
 * Scenes:
 *  - degraded: the selected pull request's full payload came back with
 *    `checks: []` and `partialSections: ['checks']` (the provider's checks
 *    read failed), while the chip-status cache still carries the CI value the
 *    backend deliberately kept alive (`ci: 'failed'`). The selected tab must
 *    keep the red glyph instead of erasing it.
 *  - clean: the same payload WITHOUT the partial flag (genuinely no CI
 *    configured). The selected tab must show no CI glyph — a stale one clears.
 *
 * Usage: node scripts/capture-selected-pr-ci-partial.mjs <baseUrl> <outDir> <prefix>
 *   prefix names the build under test (e.g. 'before' for main, 'after' for
 *   the fix), so one script serves both sides of the evidence pair.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6801'
const OUT = process.argv[3] || '../temp-screenshots/selected-pr-ci-partial'
const PREFIX = process.argv[4] || 'after'
// The prefix lands in output file names: keep it filename-safe so a stray
// argument cannot traverse out of OUT and overwrite an unrelated file.
if (!/^[A-Za-z0-9._-]+$/.test(PREFIX)) {
  console.error(`prefix must match [A-Za-z0-9._-]+, got: ${PREFIX}`)
  process.exit(2)
}
const SLOT = 'chat-pr-ci-partial'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/5127'
const OTHER_URL = 'https://github.com/kirodotdev/KiroCrew/pull/5126'

mkdirSync(OUT, { recursive: true })

const slots = () => [{
  key: SLOT,
  title: 'Keep CI glyph on degraded payloads',
  running: false,
  last_message: 'Opened the fix for the Changes panel CI glyph…',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: Math.floor(Date.now() / 1000),
  source_links: [
    { provider: 'github', number: 5127, url: PR_URL, state: 'open', ci: 'failed' },
    { provider: 'github', number: 5126, url: OTHER_URL, state: 'open', ci: 'running' },
  ],
  source_links_total: 2,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', content: 'Fix the selected-PR CI glyph', ts: Date.now() / 1000 - 600 },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: `Working on ${PR_URL} and ${OTHER_URL}.` },
  ],
}

/** Full payload for the SELECTED pull request. */
const source = partialSections => ({
  provider: 'github',
  url: PR_URL,
  number: 5127,
  title: 'fix(dashboard): keep the selected tab CI glyph on degraded payloads',
  description: 'The chip cache kept the last known CI alive; the tab must not erase it.',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: new Date().toISOString(),
  headBranch: 'fix/selected-pr-ci-partial-5127',
  baseBranch: 'main',
  headSha: 'abc1234',
  author: 'kirocrew',
  additions: 12,
  deletions: 2,
  changedFiles: 1,
  mergeable: 'mergeable',
  mergeStateStatus: 'blocked',
  commits: [{
    sha: 'abc1234', message: 'fix(dashboard): keep the selected tab CI glyph',
    author: 'kirocrew', committedAt: new Date().toISOString(), url: PR_URL,
  }],
  // Degraded: the provider's checks read failed, so the list is EMPTY while
  // `partialSections` names it. The chip cache below still knows ci: failed.
  checks: [],
  comments: [],
  files: [
    { path: 'website/src/components/PullRequestPanel.tsx', status: 'modified', additions: 12, deletions: 2, patch: '' },
  ],
  partialSections,
})

const scene = { partialSections: ['checks'] }

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

/** Fixed singleton endpoints the shell polls, answered from one table so the
 * handler below stays a two-branch dispatch rather than a stub-per-line chain. */
const FIXED_ROUTES = {
  '/api/kiro-prerequisite': { ready: true },
  '/api/status': { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/config': {},
  '/api/kirocrew-config': {},
  '/api/dashboard/branding': { bot_name: 'Kiro', avatar: '' },
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/models': { models: [], default: 'auto' },
  '/api/themes': { themes: [], installed: [] },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/chat/nav/resolve-links': { summaries: [] },
}

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    // The story is a 10px tab glyph — capture at 2x so it stays legible.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path in FIXED_ROUTES) return json(route, FIXED_ROUTES[path])
    if (path === '/api/chat/slots') return json(route, slots())
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path === '/api/source/pull-request') return json(route, source(scene.partialSections))
    if (path === '/api/source/pull-request/status') {
      return json(route, {
        statuses: {
          // The value the backend's keep-known rule preserved for the
          // degraded pull request, plus an ordinary entry for the other tab.
          [PR_URL]: { state: 'open', ci: 'failed' },
          [OTHER_URL]: { state: 'open', ci: 'running' },
        },
        refreshing: [],
        ttlSecs: 60,
      })
    }
    if (path === '/api/source/pull-request/checks') return json(route, { checks: [] })
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))

  async function load() {
    await page.addInitScript(() => {
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-pr-ci-partial')
    })
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
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

  /** Crop around the source-tab strip so the glyph pair is readable. */
  async function strip(name) {
    const tablist = page.getByRole('tablist', { name: /pull requests/i }).first()
    const box = await tablist.boundingBox().catch(() => null)
    const x = Math.max(0, (box?.x ?? 260) - 420)
    const clip = box
      ? {
        x,
        y: Math.max(0, box.y - 90),
        width: Math.min(1600 - x, box.width + 460),
        height: box.height + 170,
      }
      : { x: 240, y: 100, width: 1360, height: 220 }
    await page.screenshot({ path: `${OUT}/${name}.png`, clip })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // 1. Degraded payload: checks empty + flagged partial, chip cache knows
  //    ci: failed. Before the fix the selected tab's glyph is erased; after,
  //    it keeps the red X the backend kept alive.
  scene.partialSections = ['checks']
  await load()
  await shot(`${PREFIX}-degraded-full`)
  await strip(`${PREFIX}-degraded-strip`)

  // 2. Clean empty checks (no partial flag): no CI configured, so the tab
  //    must show NO CI glyph — the fallback must not resurrect a stale one.
  scene.partialSections = []
  await load()
  await strip(`${PREFIX}-clean-empty-strip`)

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
