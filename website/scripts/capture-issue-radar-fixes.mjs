/**
 * Screenshot harness for the three Issue Radar UX fixes in PR #1918.
 *
 * Runs the REAL built SPA (website/dist) gateway-free — every /api/** answered
 * from fixtures, no gateway, no gh, no real repo — same technique as
 * capture-embed-model.mjs. Driving fixtures is what makes these states capturable:
 * a filtered-out selection, a not-yet-mergeable PR (auto-merge armable), and the
 * cold-cache partial first page are all otherwise transient/environment-dependent.
 *
 * Captures:
 *   ir-01-hidden-issue.png   detail pane: selected issue hidden by a label filter
 *   ir-02-partial-list.png   issue list footer: "loading the rest" during cold open
 *   ir-03-automerge.png      PR detail: auto-merge offered on a not-ready PR
 *
 * Usage: node scripts/capture-issue-radar-fixes.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/ir-shots'
const PORT = 6837
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

const REPO = { owner: 'kirodotdev', repo: 'Kiro', provider: 'github', host: 'github.com' }
const REPOS = { repos: [{ ...REPO, enabled: true, permissions: { push: true, triage: true } }] }

// Two open issues; #2 has no `bug` label, so filtering to `bug` hides it.
const ISSUES = {
  ...REPO, state: 'open', from_cache: true,
  issues: [
    { number: 1, title: 'Crash on startup when config is missing', url: '#', labels: ['bug'], comments: 3, updated_at: '2026-08-05T00:00:00Z', created_at: '2026-08-01T00:00:00Z', state: 'open', author: 'alice', assignees: [] },
    { number: 2, title: 'Add a dark-mode toggle to the settings page', url: '#', labels: ['enhancement'], comments: 1, updated_at: '2026-08-06T00:00:00Z', created_at: '2026-08-02T00:00:00Z', state: 'open', author: 'bob', assignees: [] },
  ],
}
const LABELS = { ...REPO, from_cache: true, labels: [
  { name: 'bug', color: 'd73a4a', description: "Something isn't working" },
  { name: 'enhancement', color: 'a2eeef', description: 'New feature or request' },
] }

// One open PR that is NOT mergeable yet (blocked) -> auto-merge is armable.
const PULLS = {
  ...REPO, state: 'open', from_cache: true, bulk_max: 50,
  pulls: [{
    number: 42, title: 'Wire up the dark-mode toggle', url: '#', state: 'open', draft: false,
    labels: ['enhancement'], author: 'bob', updated_at: '2026-08-06T00:00:00Z', created_at: '2026-08-03T00:00:00Z',
    merged_at: null, assignees: [], requested_reviewers: [], base: 'main', head: 'dark-mode',
    head_sha: 'abc1234', additions: 120, deletions: 8, changed_files: 4,
    checks_state: 'success', checks_counts: { failure: 0, running: 0, success: 5, other: 0 },
    mergeable: true, mergeable_state: 'blocked',
  }],
}
const PR_DETAIL = {
  ...REPO, number: 42, from_cache: true,
  detail: {
    number: 42, title: 'Wire up the dark-mode toggle', body: 'Adds the toggle and persists the choice.',
    state: 'open', draft: false, merged: false, url: '#', author: 'bob', author_association: 'CONTRIBUTOR',
    created_at: '2026-08-03T00:00:00Z', updated_at: '2026-08-06T00:00:00Z', closed_at: null, merged_at: null, merged_by: null,
    comments: 2, review_comments: 1, commits: 3, additions: 120, deletions: 8, changed_files: 4,
    mergeable: true, mergeable_state: 'blocked', base: 'main', head: 'dark-mode', head_sha: 'abc1234',
    labels: [{ name: 'enhancement', color: 'a2eeef', description: '' }], assignees: [], requested_reviewers: [],
    milestone: null, auto_merge: null,
  },
  timeline: [], checks: [], from_cache: true,
}

// State the harness flips between shots.
let issuesReply = ISSUES
let issuesDelay = 0

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })
const page = await context.newPage()
await page.routeWebSocket(/\/api\/ws/, () => {})

const unmatched = new Set()
await page.route('**/api/**', async route => {
  const path = new URL(route.request().url()).pathname
  const q = new URL(route.request().url()).searchParams
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
  if (path.endsWith('/issue-radar/me')) return json(route, { login: 'owner', provider: 'github', host: 'github.com' })
  if (path.endsWith('/issue-radar/labels')) return json(route, LABELS)
  if (path.endsWith('/issue-radar/members')) return json(route, { ...REPO, members: [{ login: 'alice', role: 'admin' }], source: 'collaborators', from_cache: true })
  if (path.endsWith('/issue-radar/settings')) return json(route, { ...REPO, settings: { triage_labels: [], unlabeled_is_untriaged: true, good_first_issue_labels: [], notify_on_new_issue: false, revision: 1 } })
  if (path.endsWith('/issue-radar/issues')) {
    // first_page=1 → partial cold page; otherwise (optionally delayed) full list.
    if (q.get('first_page') === '1') return json(route, { ...ISSUES, from_cache: false, partial: true })
    if (issuesDelay) { await new Promise(r => setTimeout(r, issuesDelay)) }
    return json(route, issuesReply)
  }
  if (path.endsWith('/issue-radar/pulls')) return json(route, PULLS)
  if (path.endsWith('/issue-radar/pulls/search')) return json(route, { ...PULLS, pulls: [] })
  if (path.endsWith('/issue-radar/pull')) return json(route, PR_DETAIL)
  if (path.endsWith('/issue-radar/pull/runs')) return json(route, { ...REPO, number: 42, runs: [] })
  if (path.endsWith('/issue-radar/pull-ai') || path.endsWith('/issue-radar/issue-ai')) return json(route, { ...REPO, number: 1, summary: '', suggested_labels: [], from_cache: true })
  if (path.endsWith('/issue-radar/issue')) return json(route, { ...REPO, number: 1, detail: { number: 1, title: ISSUES.issues[0].title, body: 'Repro steps included.', state: 'open', state_reason: null, url: '#', author: 'alice', author_association: 'MEMBER', created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-05T00:00:00Z', closed_at: null, closed_by: null, comments: 3, locked: false, labels: [{ name: 'bug', color: 'd73a4a', description: '' }], assignees: [], milestone: null, reactions: null }, timeline: [], from_cache: true })
  // Boot endpoints that return LISTS (the dashboard .maps/.filters them). Default
  // object `{}` for everything else. Guessing array-vs-object wrong crashes the SPA
  // behind its error boundary (`_.filter is not a function`).
  unmatched.add(path)
  if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/approvals'
    || path === '/api/terminal/sessions' || path.endsWith('/pending')) return json(route, [])
  const listish = /(agents|sessions|projects|slots|folders|list|apps)$/.test(path)
  return json(route, listish ? [] : {})
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
const settle = (ms = 1400) => page.waitForTimeout(ms)

async function open(uiState) {
  await page.addInitScript((s) => {
    localStorage.setItem('kc:issue-radar:active-repo', JSON.stringify({ owner: 'kirodotdev', repo: 'Kiro' }))
    if (s) localStorage.setItem('kc:issue-radar:ui-state', JSON.stringify(s))
    else localStorage.removeItem('kc:issue-radar:ui-state')
  }, uiState)
  await page.goto(`http://127.0.0.1:${PORT}/issue-radar`, { waitUntil: 'domcontentloaded' })
  await settle(2200)
}

// ── 01: selected issue hidden by an active label filter ──
// mainView=issues, selectedIssue=2, selectedLabels=['bug'] — #2 lacks `bug`.
await open({ mainView: 'issues', stateFilter: 'open', selectedIssue: 2, selectedLabels: ['bug'] })
await page.screenshot({ path: `${OUT}/ir-01-hidden-issue.png` })

// ── 03: auto-merge offered on a not-ready (blocked) PR ──
await open({ mainView: 'pulls', prStateFilter: 'open', selectedPull: 42 })
await page.screenshot({ path: `${OUT}/ir-03-automerge.png` })

// ── 02: "loading the rest" partial first-page hint during a cold open ──
// Delay the full /issues so the partial first page is what paints when we shoot.
issuesReply = ISSUES
issuesDelay = 20000
await context.clearCookies()
await open({ mainView: 'issues', stateFilter: 'open' })
await settle(1500)
await page.screenshot({ path: `${OUT}/ir-02-partial-list.png` })

console.log('unmatched /api paths:', [...unmatched].join(', ') || 'none')
await context.close(); await browser.close(); server.close()
console.log('done ->', OUT)
