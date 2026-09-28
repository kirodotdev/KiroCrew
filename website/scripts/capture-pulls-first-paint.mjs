/**
 * Screenshot harness for the PR-list progressive first paint (fix/pulls-cold-open-fastpath).
 *
 * Runs the REAL built SPA (website/dist) gateway-free via the shared harness
 * helpers — `serveDist` for the static server and `stubDashboardApi` for the
 * ~25 boot endpoints — with the issue-radar routes layered on through the
 * `extra` hook. The partial first-paint state is otherwise transient: we make
 * it capturable by answering /pulls?first_page=1 immediately with one page
 * (partial) while delaying the full /pulls fetch, so the "loading the rest"
 * footer hint is what paints.
 *
 * Captures:
 *   pr-01-partial-list.png   PR list footer: "loading the rest" during a cold open
 *   pr-02-full-list.png      the same list after the full enriched set lands
 *
 * Usage: node scripts/capture-pulls-first-paint.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/pr-shots'
mkdirSync(OUT, { recursive: true })

const REPO = { owner: 'kirodotdev', repo: 'Kiro', provider: 'github', host: 'github.com' }
const REPOS = { repos: [{ ...REPO, enabled: true, permissions: { push: true, triage: true } }] }

// The newest single page — what first_page=1 returns, UN-enriched (no diff/checks).
const PR_ROW = (n, title, updated) => ({
  number: n, title, url: '#', state: 'open', draft: false,
  labels: n % 2 ? ['enhancement'] : ['bug'], author: 'bob', updated_at: updated,
  created_at: '2026-08-01T00:00:00Z', merged_at: null, assignees: [], requested_reviewers: [],
  base: 'main', head: `feature-${n}`, head_sha: `sha${n}`,
})
const FIRST_PAGE = {
  ...REPO, state: 'open', from_cache: false, partial: true, bulk_max: 50,
  pulls: [
    PR_ROW(101, 'Wire up the dark-mode toggle', '2026-08-06T00:00:00Z'),
    PR_ROW(102, 'Cache the label palette between repo switches', '2026-08-05T12:00:00Z'),
    PR_ROW(103, 'Fix flaky timeline sort on mixed offsets', '2026-08-05T09:00:00Z'),
    PR_ROW(104, 'Add a settings page for notifications', '2026-08-04T00:00:00Z'),
  ],
}
// The authoritative set — enriched (diff size + check tally), complete, no `partial`.
const enrich = (row) => ({
  ...row, additions: 40 + row.number, deletions: row.number % 7, changed_files: 3,
  checks_state: 'success', checks_counts: { failure: 0, running: 0, success: 4, other: 0 },
  mergeable: true, mergeable_state: 'clean',
})
const FULL = {
  ...REPO, state: 'open', from_cache: false, bulk_max: 50,
  pulls: [...FIRST_PAGE.pulls, PR_ROW(105, 'Older PR beyond the first page', '2026-07-30T00:00:00Z')].map(enrich),
}

const LABELS = { ...REPO, from_cache: true, labels: [
  { name: 'bug', color: 'd73a4a', description: "Something isn't working" },
  { name: 'enhancement', color: 'a2eeef', description: 'New feature or request' },
] }
const SETTINGS = { ...REPO, settings: { triage_labels: [], unlabeled_is_untriaged: true, good_first_issue_labels: [], notify_on_new_issue: false, revision: 1 } }

const { srv, base } = await serveDist()

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })
const page = await context.newPage()

let fullDelay = 0
// The issue-radar routes ride on the shared boot stub via `extra`: returning a
// truthy value marks the request handled, otherwise it falls through to the
// dashboard boot fixtures.
await stubDashboardApi(page, {
  theme: 'light',
  // `json()` returns route.fulfill()'s promise, which resolves to undefined — so
  // each branch fulfils the route THEN returns true to mark it handled (a falsy
  // return would fall through to the boot map and double-fulfill the route).
  extra: async (path, route) => {
    const q = new URL(route.request().url()).searchParams
    if (path.endsWith('/issue-radar/repos')) { await json(route, REPOS); return true }
    if (path.endsWith('/issue-radar/me')) { await json(route, { login: 'owner', provider: 'github', host: 'github.com' }); return true }
    if (path.endsWith('/issue-radar/labels')) { await json(route, LABELS); return true }
    if (path.endsWith('/issue-radar/members')) { await json(route, { ...REPO, members: [], source: 'collaborators', from_cache: true }); return true }
    if (path.endsWith('/issue-radar/settings')) { await json(route, SETTINGS); return true }
    if (path.endsWith('/issue-radar/issues')) { await json(route, { ...REPO, state: 'open', from_cache: true, issues: [] }); return true }
    if (path.endsWith('/issue-radar/pulls')) {
      // first_page=1 → the partial cold page immediately; otherwise the (delayed) full set.
      if (q.get('first_page') === '1') { await json(route, FIRST_PAGE); return true }
      if (fullDelay) { await new Promise(r => setTimeout(r, fullDelay)) }
      await json(route, FULL); return true
    }
    if (path.endsWith('/issue-radar/pulls/search')) { await json(route, { ...FULL, pulls: [] }); return true }
    return false
  },
})

async function open(uiState) {
  await page.addInitScript((s) => {
    localStorage.setItem('kc:issue-radar:active-repo', JSON.stringify({ owner: 'kirodotdev', repo: 'Kiro' }))
    if (s) localStorage.setItem('kc:issue-radar:ui-state', JSON.stringify(s))
    else localStorage.removeItem('kc:issue-radar:ui-state')
  }, uiState)
  await page.goto(`${base}/issue-radar`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
}

// ── 01: "loading the rest" — the partial first page paints while /pulls is delayed ──
fullDelay = 20000
await open({ mainView: 'pulls', prStateFilter: 'open' })
await page.waitForTimeout(1500)
await page.screenshot({ path: `${OUT}/pr-01-partial-list.png` })

// ── 02: the full enriched set after it lands (diff bars + check tallies, no hint) ──
fullDelay = 0
await context.clearCookies()
await open({ mainView: 'pulls', prStateFilter: 'open' })
await page.waitForTimeout(1800)
await page.screenshot({ path: `${OUT}/pr-02-full-list.png` })

await context.close(); await browser.close(); srv.close()
console.log('done ->', OUT)
