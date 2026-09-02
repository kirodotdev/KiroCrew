/**
 * Screenshot harness for the selected pull request's merged chip status.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server,
 * with the gateway API answered by the shared stub — no gateway, no dashboard
 * token, no provider calls. Only the three source endpoints this surface needs
 * are named here, via the stub's `extra` hook.
 *
 * Scene: two pull requests in the source strip. The SELECTED one has a full
 * payload whose merge pair is settled (`mergeable: 'conflicting'`,
 * `mergeStateStatus: 'dirty'`), and a chip-status cache entry carrying the same
 * pair. `statusByUrl` rebuilt that entry from the payload alone, so the pair was
 * dropped for the selected source only; `selectedSourceStatus` now layers the
 * payload over the cached entry field by field instead.
 *
 * The panel renders no glyph from that pair today, so this frame is
 * no-regression evidence rather than a before/after delta: the strip, the
 * lifecycle/CI glyphs, the header badge and the merge-blocker banner must all
 * still render. The data invariant itself is pinned by `selectedSourceStatus`
 * unit tests, which a screenshot cannot show.
 *
 * Usage: node scripts/capture-selected-pr-status-merge.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/selected-pr-status-merge'
const PREFIX = process.argv[3] || 'after'
// The prefix lands in output file names: keep it filename-safe so a stray
// argument cannot traverse out of OUT and overwrite an unrelated file.
if (!/^[A-Za-z0-9._-]+$/.test(PREFIX)) {
  console.error(`prefix must match [A-Za-z0-9._-]+, got: ${PREFIX}`)
  process.exit(2)
}
const SLOT = 'chat-pr-status-merge'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/6999'
const OTHER_URL = 'https://github.com/kirodotdev/KiroCrew/pull/6998'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Keep the merge pair on the selected tab',
  running: false,
  last_message: 'Opened the Changes panel on the conflicting pull request…',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: Math.floor(Date.now() / 1000),
  source_links: [
    { provider: 'github', number: 6999, url: PR_URL, state: 'open', ci: 'failed', mergeable: 'conflicting', mergeStateStatus: 'dirty' },
    { provider: 'github', number: 6998, url: OTHER_URL, state: 'open', ci: 'running' },
  ],
  source_links_total: 2,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', content: 'Show me the conflicting pull request', ts: Date.now() / 1000 - 600 },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: `Working on ${PR_URL} and ${OTHER_URL}.` },
  ],
}

/** Full payload for the SELECTED pull request: merge pair settled, CI failed. */
const source = {
  provider: 'github',
  url: PR_URL,
  number: 6999,
  title: 'fix(dashboard): keep the settled merge pair on the selected tab',
  description: 'The selected tab rebuilt its chip status from the payload alone, so it lost every field the payload does not mention.',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: new Date().toISOString(),
  headBranch: 'fix/pr-panel-selected-status-merge',
  baseBranch: 'main',
  headSha: 'abc1234',
  author: 'kirocrew',
  additions: 46,
  deletions: 14,
  changedFiles: 2,
  mergeable: 'conflicting',
  mergeStateStatus: 'dirty',
  commits: [{
    sha: 'abc1234', message: 'fix(dashboard): merge the selected status field by field',
    author: 'kirocrew', committedAt: new Date().toISOString(), url: PR_URL,
  }],
  checks: [
    { name: 'Frontend Tests', workflow: 'CI', status: 'COMPLETED', conclusion: 'FAILURE', bucket: 'failed', url: `${PR_URL}/checks`, startedAt: '', completedAt: '' },
  ],
  comments: [],
  files: [
    { path: 'website/src/components/PullRequestPanel.tsx', status: 'modified', additions: 38, deletions: 14, patch: '' },
    { path: 'website/src/test/PullRequestPanel.test.tsx', status: 'modified', additions: 34, deletions: 0, patch: '' },
  ],
  partialSections: [],
}

/** The chip-status cache: the SELECTED entry carries the settled merge pair,
 *  which rebuilding the record from the payload alone dropped here and only
 *  here. */
const statuses = {
  [PR_URL]: { state: 'open', ci: 'failed', mergeable: 'conflicting', mergeStateStatus: 'dirty' },
  [OTHER_URL]: { state: 'open', ci: 'running' },
}

/** Only the endpoints this surface needs; everything else is the shared stub.
 *  Returns TRUE once handled -- `json()` resolves to undefined, so returning it
 *  alone would read as unhandled and the stub would fulfil the route twice. */
const sourceRoutes = async (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return await json(route, detail), true
  if (path === '/api/source/pull-request') return await json(route, source), true
  if (path === '/api/source/pull-request/status') {
    return await json(route, { statuses, refreshing: [], ttlSecs: 60 }), true
  }
  if (path === '/api/source/pull-request/checks') {
    return await json(route, { checks: source.checks }), true
  }
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1500, height: 900 } })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    extra: sourceRoutes,
    localStorageEntries: { 'mc-active-slot': SLOT },
  })

  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const opener = page.getByRole('button', { name: 'Open activity panel' })
  if (await opener.count()) {
    await opener.first().click().catch(() => {})
    await page.waitForTimeout(1200)
  }
  const changes = page.getByRole('button', { name: /^Changes/ })
  if (await changes.count()) {
    await changes.first().click().catch(() => {})
    await page.waitForTimeout(2500)
  }

  // Assert on the RENDERED strip rather than trusting the frame: a stale bundle
  // or a panel that never mounted would otherwise be screenshotted silently.
  const tablist = page.getByRole('tablist', { name: /pull requests/i }).first()
  const tabs = await tablist.count()
  const strip = tabs ? (await tablist.innerText()).replace(/\s+/g, ' ').trim() : '(no strip)'
  const conflictMentions = await page.getByText(/conflict/i).count()
  console.log(`tablist:${tabs} strip:"${strip}" conflictMentions:${conflictMentions}`)

  await page.screenshot({ path: `${OUT}/${PREFIX}-full-app.png` })
  console.log('wrote', `${OUT}/${PREFIX}-full-app.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
