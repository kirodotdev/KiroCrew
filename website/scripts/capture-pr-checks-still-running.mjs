/**
 * Screenshot harness: a pull request whose checks are still running.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server, with
 * the gateway API answered by the shared stub — no gateway, no provider calls.
 *
 * Two scenes, each in light and dark, each asserted on the rendered page before
 * its frame is written:
 *
 * - `failing`: 101 checks, 99 passed, 1 failed, 1 still running, `blocked`. The
 *   Checks tab reads "1 running · 1 failed", the merge banner is red and names
 *   both counts, and the sidebar chip keeps its red × AND shows the static
 *   pending dot.
 * - `waiting`: 101 checks, 100 passed, 1 running, nothing failed, `blocked`.
 *   The tab keeps "Checks running 100/101" and the "Merge blocked" banner says
 *   checks are still running.
 *
 * Usage: node scripts/capture-pr-checks-still-running.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pr-checks-still-running'
const SLOT = 'chat-pr-checks-running'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/7001'

mkdirSync(OUT, { recursive: true })

const check = (name, bucket, conclusion, status = 'COMPLETED') => ({
  name, workflow: 'CI', status, conclusion, bucket, url: `${PR_URL}/checks`, startedAt: '', completedAt: '',
})
const passed = n => Array.from({ length: n }, (_, i) => check(`Backend Tests (shard ${i + 1})`, 'passed', 'SUCCESS'))
const running = check('E2E (shard 3/4)', 'pending', '', 'IN_PROGRESS')

const SCENES = {
  failing: {
    checks: [check('Sensitive Change Review', 'failed', 'FAILURE'), running, ...passed(99)],
    chip: { ci: 'failed', ciPending: '1' },
    async assert(page) {
      await page.getByRole('tab', { name: /Checks 1 running · 1 failed/ }).waitFor()
      await page.getByRole('alert').filter({ hasText: 'Checks failed' }).getByText(/1 failed and 1 still running, of 101 checks/).waitFor()
      const inFlight = page.getByRole('img', { name: 'Checks failed; others still running' })
      if ((await inFlight.count()) < 1) throw new Error('sidebar chip lost its still-running dot')
      if (!(await page.getByLabel('Checks failed', { exact: true }).count())) {
        throw new Error('finished-failure chip no longer renders a bare ×')
      }
    },
  },
  waiting: {
    checks: [running, ...passed(100)],
    chip: { ci: 'running' },
    async assert(page) {
      await page.getByRole('tab', { name: /Checks running 100\/101/ }).waitFor()
      await page.getByRole('alert').filter({ hasText: 'Merge blocked' }).getByText(/Checks are still running/).waitFor()
    },
  },
}

function fixtures(scene) {
  const { checks, chip } = SCENES[scene]
  const live = { state: 'open', mergeable: 'mergeable', mergeStateStatus: 'blocked', ...chip }
  const slots = [{
    key: SLOT,
    title: 'Git tab workspace folder auto detection',
    running: false,
    last_message: 'Pushed the fix; waiting on CI…',
    messages: 2,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    modified: Math.floor(Date.now() / 1000),
    source_links: [
      { provider: 'github', number: 7001, url: PR_URL, ...live },
    ],
    source_links_total: 1,
  }, {
    key: 'chat-pr-finished',
    title: 'Git branch management for changes tab',
    running: false,
    last_message: 'The UX reviewer blocked the PR.',
    messages: 2,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    modified: Math.floor(Date.now() / 1000) - 60,
    source_links: [
      { provider: 'github', number: 7000, url: 'https://github.com/kirodotdev/KiroCrew/pull/7000', state: 'open', ci: 'failed' },
    ],
    source_links_total: 1,
  }]
  const detail = {
    running: false, has_more: false, total: 2, queue: [],
    messages: [
      { role: 'user', content: 'Is the PR green yet?', ts: Date.now() / 1000 - 600 },
      { role: 'assistant', ts: Date.now() / 1000 - 60, content: `Watching ${PR_URL}.` },
    ],
  }
  const source = {
    provider: 'github', url: PR_URL, number: 7001,
    title: 'feat(git): list every repo a chat works in on the Git tab',
    description: '', state: 'OPEN', draft: false, mergedAt: '', updatedAt: new Date().toISOString(),
    headBranch: 'feat/git-tab-auto-repos', baseBranch: 'main', headSha: 'abc1234', author: 'kirocrew',
    additions: 4486, deletions: 136, changedFiles: 1,
    mergeable: 'mergeable', mergeStateStatus: 'blocked',
    commits: [{ sha: 'abc1234', message: 'feat(git): auto repos', author: 'kirocrew', committedAt: new Date().toISOString(), url: PR_URL }],
    checks, comments: [],
    files: [{ path: 'website/src/pages/GitTab.tsx', status: 'modified', additions: 10, deletions: 2, patch: '' }],
    partialSections: [],
  }
  const statuses = { [PR_URL]: live }
  const routes = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) return await json(route, detail), true
    if (path === '/api/source/pull-request') return await json(route, source), true
    if (path === '/api/source/pull-request/status') return await json(route, { statuses, refreshing: [], ttlSecs: 60 }), true
    if (path === '/api/source/pull-request/checks') return await json(route, { checks }), true
    return false
  }
  return { slots, routes }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  try {
    for (const scene of Object.keys(SCENES)) {
      for (const theme of ['light', 'dark']) {
        const { slots, routes } = fixtures(scene)
        const context = await browser.newContext({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 2 })
        const page = await context.newPage()
        logPageProblems(page)
        await stubDashboardApi(page, {
          slots, theme, extra: routes,
          localStorageEntries: { 'mc-active-slot': SLOT, 'mc-sidebar-pinned': 'true', 'mc-session-stale-collapse-ms': '0' },
        })
        await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
        await page.waitForTimeout(2500)
        const opener = page.getByRole('button', { name: 'Open activity panel' })
        if (await opener.count()) { await opener.first().click().catch(() => {}); await page.waitForTimeout(1200) }
        const changes = page.getByRole('button', { name: /^Changes/ })
        if (await changes.count()) { await changes.first().click().catch(() => {}); await page.waitForTimeout(2500) }

        // Assert on the RENDERED page, so a stale bundle is never screenshotted.
        await SCENES[scene].assert(page)

        await page.mouse.move(700, 850)
        await page.screenshot({ path: `${OUT}/${scene}-${theme}-full-app.png` })
        console.log('wrote', `${OUT}/${scene}-${theme}-full-app.png`)
        await context.close()
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
