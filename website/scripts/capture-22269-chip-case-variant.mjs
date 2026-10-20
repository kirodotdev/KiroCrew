/**
 * Screenshot harness for issue #22269: clicking a sidebar PR chip must activate
 * the Changes tab the panel already has for that pull request, not add another.
 *
 * Two chips on one session row, each clicked twice:
 *  - #648, whose chip url matches the panel's listed url exactly (the control:
 *    repeated clicks on it must leave the strip unchanged);
 *  - #634, whose chip spells the repository in lowercase while the agent's
 *    transcript mention, and so the panel's listed url, capitalizes it. GitHub
 *    treats the two spellings as one pull request.
 *
 * After each click the harness records the Changes strip's tabs and how many
 * times each pull request was requested, and screenshots the panel.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures (gateway-free). `DIST` selects the build.
 *
 * Usage: DIST=<dist dir> node scripts/capture-22269-chip-case-variant.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/22269-chip-case-variant'
const ACTIVE = 'chat-a'
const OTHER = 'chat-b'
const LISTED_634 = 'https://github.com/kirodotdev/KiroCrew/pull/634'
const CHIP_634 = 'https://github.com/kirodotdev/kirocrew/pull/634'
const PR_648 = 'https://github.com/kirodotdev/KiroCrew/pull/648'

mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)

const slots = [
  {
    key: ACTIVE, title: 'Draft the release notes', running: false, messages: 2,
    agent: 'kirocrew', modified: now, last_ts: '2026-10-20T00:10:00Z', folder_id: '',
    last_message: 'Grouped the entries by area.',
  },
  {
    key: OTHER, title: 'Session flyout for the collapsed sidebar', running: false, messages: 3,
    agent: 'kirocrew', modified: now - 900, last_ts: '2026-10-20T00:00:00Z', folder_id: '',
    last_message: 'Both pull requests are up.',
    source_links: [
      { provider: 'github', number: 634, url: CHIP_634, state: 'open', ci: 'passed', kind: 'change' },
      { provider: 'github', number: 648, url: PR_648, state: 'open', ci: 'passed', kind: 'change' },
    ],
    source_links_total: 2,
  },
]

const detail = {
  running: false, has_more: false, total: 3, queue: [],
  messages: [
    { role: 'assistant', content: `Opened ${LISTED_634} and ${PR_648}.`, ts: now - 1800 },
    { role: 'user', content: `Can you rebase ${CHIP_634}?`, ts: now - 1200 },
    { role: 'assistant', content: 'Rebased and pushed.', ts: now - 900 },
  ],
}

const pr = (url, number, title) => ({
  provider: 'github', url, number, title,
  description: 'Fixture pull request.', state: 'OPEN', draft: false, mergedAt: '',
  updatedAt: new Date().toISOString(), headBranch: `feat/${number}`, baseBranch: 'main', headSha: 'a79a20f7',
  author: 'diwu', additions: 10, deletions: 2, changedFiles: 1,
  mergeable: 'mergeable', mergeStateStatus: 'clean', commits: [], checks: [], comments: [],
  files: [{ path: 'website/src/pages/ChatSidebar.tsx', status: 'modified', additions: 10, deletions: 2, patch: '' }],
})

const prRequests = []
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  if (path === '/api/source/pull-request') {
    const url = JSON.parse(route.request().postData() || '{}').url || ''
    prRequests.push(url)
    const n = Number(url.split('/').pop())
    return json(route, pr(url, n, n === 634 ? 'feat(chat): session hover flyout' : 'fix(chat): flyout focus ring')), true
  }
  if (path === '/api/source/pull-request/checks') return json(route, { checks: [] }), true
  if (path === '/api/source/pull-request/status') return json(route, { statuses: {}, refreshing: [], ttlSecs: 60 }), true
  return false
}

async function strip(page) {
  return page.locator('[role="tablist"][aria-label="Pull requests"] [role="tab"]')
    .evaluateAll(els => els.map(e => e.getAttribute('title')))
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-sidebar-pinned', 'true')
  }, ACTIVE)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const report = []
  for (const [label, url] of [['648-exact', PR_648], ['634-other-case', CHIP_634]]) {
    const chip = page.locator(`a[title^="Open ${url} in the side panel"]`).first()
    await chip.waitFor({ state: 'visible', timeout: 15000 })
    for (const n of [1, 2]) {
      await chip.click()
      await page.locator('[role="tablist"][aria-label="Pull request sections"]').first()
        .waitFor({ state: 'visible', timeout: 15000 })
      await page.waitForTimeout(800)
      await page.screenshot({ path: `${OUT}/${label}-click${n}.png` })
      report.push({ click: `${label} #${n}`, tabs: await strip(page), requests: [...prRequests] })
    }
  }
  console.log(JSON.stringify(report, null, 1))
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
