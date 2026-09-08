/**
 * Screenshot harness for the Git panel's per-repo grouping.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend).
 *
 * A project directory that is not itself a repository but contains several — one
 * repo per package — used to render an empty panel, because the backend resolved
 * the repo upward only. `/api/project/git/status` now returns a `repos` array,
 * and the panel renders one header per repo with each row anchored to its own
 * repo root.
 *
 * Frames:
 *   01-multi-repo   four sibling repos, grouped with per-repo headers + counts
 *   02-single-repo  the unchanged single-repo shape (no headers, one flat list)
 *   03-clean        multi-repo workspace with a clean tree
 *   04-truncated    the shared row budget exhausted, with its notice
 *   05-skipped      a repo refused for declaring a content-filter driver
 *   06-capped       discovery stopped at a bound, so repos are missing
 *
 * The `01` frame is the delta: run against origin/main to see the same fixture
 * render as an empty panel.
 *
 * Usage: node scripts/capture-multi-repo-git-panel.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/multi-repo-git-status'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const SLOT = 'slot-1'
const WS = '/workplace/demo/MyWorkspace'

const file = (path, status, staged, additions, deletions, repoRoot) => ({
  path, status, staged, additions, deletions, repoRoot,
})

/** Four sibling package repos, the shape a per-package workspace produces. */
const REPOS = [
  {
    root: `${WS}/src/AuthService`,
    name: 'src/AuthService',
    branch: 'trunk',
    files: [
      file('src/handlers/login.py', 'M', false, 42, 7, `${WS}/src/AuthService`),
      file('src/handlers/session.py', 'M', true, 18, 3, `${WS}/src/AuthService`),
      file('test/test_login.py', 'A', false, 96, 0, `${WS}/src/AuthService`),
    ],
  },
  {
    root: `${WS}/src/BillingWorker`,
    name: 'src/BillingWorker',
    branch: 'feature/retry-backoff',
    ahead: 2,
    files: [
      file('worker/retry.py', 'M', false, 11, 4, `${WS}/src/BillingWorker`),
      file('worker/legacy_poller.py', 'D', true, 0, 213, `${WS}/src/BillingWorker`),
    ],
  },
  {
    root: `${WS}/src/WebFrontend`,
    name: 'src/WebFrontend',
    branch: 'trunk',
    files: [
      // Same repo-relative path as AuthService's, which is exactly why every row
      // carries its own repoRoot.
      file('src/handlers/login.py', 'M', false, 5, 5, `${WS}/src/WebFrontend`),
      file('src/components/Banner.tsx', '?', false, undefined, undefined, `${WS}/src/WebFrontend`),
    ],
  },
  {
    root: `${WS}/src/SharedModels`,
    name: 'src/SharedModels',
    branch: 'trunk',
    behind: 3,
    files: [
      file('models/account.py', 'M', true, 8, 1, `${WS}/src/SharedModels`),
    ],
  },
]

const MULTI = {
  repo: true,
  files: REPOS.flatMap(r => r.files),
  repos: REPOS,
}

const SINGLE = {
  repo: true,
  repoRoot: `${WS}/src/AuthService`,
  branch: 'trunk',
  ahead: 1,
  files: REPOS[0].files.map(f => ({ ...f, repoRoot: `${WS}/src/AuthService` })),
}

const CLEAN = {
  repo: true,
  files: [],
  repos: REPOS.map(r => ({ ...r, files: [] })),
}

/** The row budget spent inside the first repo, so a later repo shows 0 rows. */
const bulk = Array.from({ length: 500 }, (_, i) =>
  file(`src/generated/model_${String(i).padStart(3, '0')}.py`, 'M', false, 3, 1, `${WS}/src/AuthService`))

const TRUNCATED = {
  repo: true,
  truncated: true,
  files: bulk,
  repos: [
    { ...REPOS[0], files: bulk },
    { ...REPOS[1], files: [] },
    { ...REPOS[2], files: [] },
  ],
}

/** Discovery stopped at a bound, so the repo list is not the whole workspace. */
const CAPPED = {
  repo: true,
  reposTruncated: true,
  files: REPOS[0].files,
  repos: [REPOS[0], REPOS[1]],
}

/** A repo whose own config names a filter driver is refused, not read. */
const SKIPPED = {
  repo: true,
  files: REPOS[0].files,
  repos: [
    REPOS[0],
    { ...REPOS[1], files: [], refused: true },
    REPOS[3],
  ],
}

const LOG = {
  repo: true,
  commits: [
    {
      sha: '9f2c1ab', message: 'fix(auth): retry the token refresh once on 401',
      author: 'dhasman', date: new Date(Date.now() - 3600_000).toISOString(), isHead: true,
    },
    {
      sha: '3ad7e40', message: 'feat(billing): exponential backoff for the retry worker',
      author: 'dhasman', date: new Date(Date.now() - 26 * 3600_000).toISOString(), isHead: false,
    },
  ],
}

const SLOTS = [{
  key: SLOT,
  title: 'Multi-repo workspace',
  running: false,
  messages: 6,
  agent: 'kirocrew',
  project: WS,
  modified: Math.floor(Date.now() / 1000),
  last_ts: new Date().toISOString(),
  folder_id: '',
}]

/** Git endpoints the panel reaches for; `status` varies per frame. */
const gitRoutes = (status, log = LOG) => async (path, route) => {
  if (path === '/api/project/git/status') return json(route, status), true
  if (path === '/api/project/git/log') return json(route, log), true
  if (path === '/api/project/git') {
    return json(route, { repo: true, repoRoot: WS, branch: '', path: WS }), true
  }
  if (path.startsWith('/api/chat/slot/')) {
    return json(route, { slot: SLOT, project: WS, messages: [] }), true
  }
  return false
}

/** A workspace root is not a repo, so the log endpoint answers with nothing. */
const NO_LOG = { repo: false, commits: [] }

async function frame(browser, base, name, status, log = LOG, scrollToEnd = false) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(page)
  await stubDashboardApi(page, { slots: SLOTS, theme: 'dark', extra: gitRoutes(status, log) })
  await page.addInitScript(([slot, ws]) => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-project-dir:' + slot, ws)
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
      tabs: [{ id: 'git', kind: 'git', title: 'Git' }],
      activeId: 'git',
    }))
  }, [SLOT, WS])
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  if (scrollToEnd) {
    // The truncation notice sits after every group row, so a 500-row fixture
    // puts it below the fold; scroll the panel's own scroller to its end.
    await page.evaluate(() => {
      const panes = [...document.querySelectorAll('div.overflow-y-auto')]
      const pane = panes.find(p => p.scrollHeight > p.clientHeight + 40 && /model_/.test(p.textContent || ''))
      if (pane) pane.scrollTop = pane.scrollHeight
    })
    await page.waitForTimeout(400)
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
  console.log('wrote', `${OUT}/${PREFIX}-${name}.png`)
  await page.close()
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

await frame(browser, base, '01-multi-repo', MULTI, NO_LOG)
await frame(browser, base, '02-single-repo', SINGLE)
await frame(browser, base, '03-clean', CLEAN, NO_LOG)
await frame(browser, base, '04-truncated', TRUNCATED, NO_LOG, true)
await frame(browser, base, '05-skipped', SKIPPED, NO_LOG)
await frame(browser, base, '06-capped', CAPPED, NO_LOG)

await browser.close()
srv.close()
