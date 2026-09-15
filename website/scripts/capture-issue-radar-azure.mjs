/**
 * Screenshot harness for Azure DevOps as a third Issue Radar provider.
 *
 * Runs the REAL built SPA (website/dist) gateway-free — every /api/** answered
 * from fixtures, no gateway, no `az` CLI, no Azure DevOps organization — the same
 * technique as capture-issue-radar-fixes.mjs. Driving fixtures is what makes
 * these frames capturable at all: `az` is not installed on the build host and an
 * Azure DevOps organization is not something CI holds a credential for, so a live
 * capture is impossible here rather than merely inconvenient.
 *
 * Captures:
 *   az-01-work-items.png     work item list + rail: the Azure mark, the
 *                            {org}/{project} identity, the read-only tag
 *   az-02-pulls.png          pull request list on the same repo
 *   az-03-github-control.png the SAME surface on GitHub, as the control — the
 *                            point of a per-provider descriptor table is that
 *                            GitHub's rendering did not move
 *
 * Usage: node scripts/capture-issue-radar-azure.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/az-shots'
const PORT = 6841
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' }
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) { res.writeHead(403); res.end(); return }
  if (!existsSync(file) || path === '/') file = join(DIST, 'index.html')
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch { res.writeHead(404); res.end() }
})
await new Promise(r => server.listen(PORT, '127.0.0.1', r))

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

// `owner` carries {organization}/{project} and `repo` is the git repository —
// the same overloading GitLab already applies to nested group paths.
const AZ = { owner: 'contoso/Widgets', repo: 'widget-service', provider: 'azure', host: 'dev.azure.com' }
const GH = { owner: 'kirodotdev', repo: 'Kiro', provider: 'github', host: 'github.com' }

// push/triage false is what azure_client._permissions returns, so the rail's
// read-only tag is part of the frame rather than an accident of the fixture.
const REPOS = {
  repos: [
    { ...AZ, enabled: true, permissions: { push: false, triage: false } },
    { ...GH, enabled: true, permissions: { push: true, triage: true } },
  ],
}

const WORK_ITEMS = [
  { number: 1841, title: 'Checkout retries the payment twice on a 504', url: '#', labels: ['bug', 'area/payments'], comments: 4, updated_at: '2026-08-19T00:00:00Z', created_at: '2026-08-11T00:00:00Z', state: 'open', author: 'ada@contoso.com', assignees: ['ada@contoso.com'] },
  { number: 1852, title: 'Widget catalogue paginates past the last page', url: '#', labels: ['bug'], comments: 1, updated_at: '2026-08-18T00:00:00Z', created_at: '2026-08-14T00:00:00Z', state: 'open', author: 'grace@contoso.com', assignees: [] },
  { number: 1863, title: 'Add a bulk export to the reporting view', url: '#', labels: ['enhancement'], comments: 0, updated_at: '2026-08-17T00:00:00Z', created_at: '2026-08-15T00:00:00Z', state: 'open', author: 'grace@contoso.com', assignees: [] },
]
const GH_ISSUES = [
  { number: 91, title: 'Crash on startup when config is missing', url: '#', labels: ['bug'], comments: 3, updated_at: '2026-08-19T00:00:00Z', created_at: '2026-08-11T00:00:00Z', state: 'open', author: 'alice', assignees: ['alice'] },
  { number: 94, title: 'Add a dark-mode toggle to the settings page', url: '#', labels: ['enhancement'], comments: 1, updated_at: '2026-08-18T00:00:00Z', created_at: '2026-08-14T00:00:00Z', state: 'open', author: 'bob', assignees: [] },
]

const LABELS = [
  { name: 'bug', color: 'd73a4a', description: "Something isn't working" },
  { name: 'enhancement', color: 'a2eeef', description: 'New feature or request' },
  { name: 'area/payments', color: '888888', description: '' },
]

const AZ_PULLS = [{
  number: 88, title: 'Retry the payment capture idempotently', url: '#', state: 'open', draft: false,
  labels: ['bug'], author: 'ada@contoso.com', updated_at: '2026-08-19T00:00:00Z', created_at: '2026-08-16T00:00:00Z',
  merged_at: null, assignees: [], requested_reviewers: [], base: 'main', head: 'idempotent-capture',
  head_sha: 'a1b2c3d', additions: 96, deletions: 14, changed_files: 5,
  checks_state: 'success', checks_counts: { failure: 0, running: 0, success: 4, other: 0 },
  mergeable: true, mergeable_state: 'mergeable',
}]
const GH_PULLS = [{
  number: 42, title: 'Wire up the dark-mode toggle', url: '#', state: 'open', draft: false,
  labels: ['enhancement'], author: 'bob', updated_at: '2026-08-19T00:00:00Z', created_at: '2026-08-16T00:00:00Z',
  merged_at: null, assignees: [], requested_reviewers: [], base: 'main', head: 'dark-mode',
  head_sha: 'abc1234', additions: 120, deletions: 8, changed_files: 4,
  checks_state: 'success', checks_counts: { failure: 0, running: 0, success: 5, other: 0 },
  mergeable: true, mergeable_state: 'mergeable',
}]

/** Which fixture repo a request is about, read from its own query string.
 *
 * Azure is the only provider whose `owner` carries a `/`, so the fallback keeps
 * working for a request that names no provider at all. */
const refOf = q => (q.get('provider') === 'azure' || (q.get('owner') || '').includes('/') ? AZ : GH)

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
const page = await context.newPage()
await page.routeWebSocket(/\/api\/ws/, () => {})

const unmatched = new Set()
await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  const q = url.searchParams
  const ref = refOf(q)
  const isAz = ref === AZ
  // ── boot endpoints ──
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/kiro-prerequisite') return json(route, { platform: 'gateway', installed: true, authenticated: true, ready: true, initial_setup_complete: true, can_auto_install: false, can_login: true, repair_required: false, docs_url: '', setup_allowed: false, operation: { status: 'idle', message: '' } })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '' })
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (path === '/api/dashboard/config') return json(route, {})
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/status') return json(route, { sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, uptime: 1000, version: '0.1.0' })
  if (path === '/api/chat/slots') return json(route, [])
  if (path === '/api/chat/folders') return json(route, [])
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  // ── issue-radar ──
  if (path.endsWith('/issue-radar/repos')) return json(route, REPOS)
  if (path.endsWith('/issue-radar/me')) return json(route, { login: isAz ? 'ada@contoso.com' : 'owner', provider: ref.provider, host: ref.host })
  if (path.endsWith('/issue-radar/labels')) return json(route, { ...ref, from_cache: true, labels: LABELS })
  if (path.endsWith('/issue-radar/members')) return json(route, { ...ref, members: [{ login: isAz ? 'ada@contoso.com' : 'alice', role: 'admin' }], source: 'collaborators', from_cache: true })
  if (path.endsWith('/issue-radar/settings')) return json(route, { ...ref, settings: { triage_labels: [], unlabeled_is_untriaged: true, good_first_issue_labels: [], notify_on_new_issue: false, revision: 1 } })
  if (path.endsWith('/issue-radar/issues')) return json(route, { ...ref, state: 'open', from_cache: true, issues: isAz ? WORK_ITEMS : GH_ISSUES })
  if (path.endsWith('/issue-radar/pulls')) return json(route, { ...ref, state: 'open', from_cache: true, bulk_max: 50, pulls: isAz ? AZ_PULLS : GH_PULLS })
  if (path.endsWith('/issue-radar/pulls/search')) return json(route, { ...ref, state: 'open', from_cache: true, pulls: [] })
  if (path.endsWith('/issue-radar/pull/runs')) return json(route, { ...ref, number: 88, runs: [] })
  if (path.endsWith('/issue-radar/pull-ai') || path.endsWith('/issue-radar/issue-ai')) return json(route, { ...ref, number: 1841, summary: '', suggested_labels: [], from_cache: true })
  if (path.endsWith('/issue-radar/recent-repos')) return json(route, { repos: [] })
  // Boot endpoints that return LISTS (the dashboard .maps/.filters them). Default
  // object `{}` for everything else. Guessing array-vs-object wrong crashes the
  // SPA behind its error boundary (`_.filter is not a function`).
  unmatched.add(path)
  if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/approvals'
    || path === '/api/terminal/sessions' || path.endsWith('/pending')) return json(route, [])
  const listish = /(agents|sessions|projects|slots|folders|list|apps)$/.test(path)
  return json(route, listish ? [] : {})
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
const settle = (ms = 2400) => page.waitForTimeout(ms)

async function open(active, ui) {
  await page.addInitScript((s) => {
    localStorage.setItem('kc:issue-radar:active-repo', JSON.stringify(s.active))
    if (s.ui) localStorage.setItem('kc:issue-radar:ui-state', JSON.stringify(s.ui))
    else localStorage.removeItem('kc:issue-radar:ui-state')
  }, { active, ui })
  await page.goto(`http://127.0.0.1:${PORT}/issue-radar`, { waitUntil: 'domcontentloaded' })
  await settle()
}

const AZ_ACTIVE = { owner: AZ.owner, repo: AZ.repo, provider: 'azure', host: 'dev.azure.com' }
const GH_ACTIVE = { owner: GH.owner, repo: GH.repo, provider: 'github', host: 'github.com' }

// ── 01: the work item list on an Azure DevOps repo ──
await open(AZ_ACTIVE, { mainView: 'issues', stateFilter: 'open' })
await page.screenshot({ path: `${OUT}/az-01-work-items.png` })

// ── 02: the pull request list on the same repo ──
await open(AZ_ACTIVE, { mainView: 'pulls', prStateFilter: 'open' })
await page.screenshot({ path: `${OUT}/az-02-pulls.png` })

// ── 03: the control — GitHub's rendering of the same surface ──
await open(GH_ACTIVE, { mainView: 'issues', stateFilter: 'open' })
await page.screenshot({ path: `${OUT}/az-03-github-control.png` })

// ── 04: the repo switcher open, both providers in one frame.
// The single clearest evidence for the per-provider mark table: `owner/repo`
// alone is identical across providers, so the marks are the only thing telling
// these two rows apart. Cropped to the switcher — a full page here would be
// mostly the empty workspace behind the popover.
await open(AZ_ACTIVE, { mainView: 'issues', stateFilter: 'open' })
await page.getByTestId('repo-path-label').click()
await page.waitForTimeout(700)
await page.screenshot({ path: `${OUT}/az-04-repo-switcher.png`, clip: { x: 258, y: 46, width: 340, height: 290 } })

console.log('unmatched /api paths:', [...unmatched].join(', ') || 'none')
await context.close(); await browser.close(); server.close()
console.log('done ->', OUT)
