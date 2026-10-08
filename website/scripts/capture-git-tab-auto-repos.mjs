/**
 * Screenshot harness for the Git tab listing every repository a chat works in.
 *
 * Runs the REAL built SPA (website/dist) with /api/** answered from fixtures.
 * The side panel is seeded open on the Git tab, so no auto-open is involved
 * (that path only fires for a project that is itself a repository).
 *
 * Five frames:
 *   workspace   project = the shared workspace (not a repo); three repos the
 *               agent touched, two dirty -> three sections, dirty ones open
 *   project     project IS a repo: it comes first and wears the Project tag
 *   empty       nothing found yet -> "No Git repositories found for this chat yet."
 *   listing     GET /api/project/git/repos fails -> the repos_failed ErrorNotice
 *   status      two repos; the second's status route fails -> that section
 *               opens itself with its status ErrorNotice in the body
 *
 * Each frame asserts the rendered section paths (and, for the failure frames,
 * the notice) before it is written.
 *
 * Usage: node scripts/capture-git-tab-auto-repos.mjs <outDir>
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/git-tab-auto-repos'
const SLOT = 'chat-git-auto-repos'
const WORKSPACE = '/home/user/.kiro/crew/workspace'
const SERVICE = '/home/user/workspace/payments-service'
const INFRA = '/home/user/workspace/payments-infra'
const DOCS = '/home/user/workspace/team-docs'
mkdirSync(OUT, { recursive: true })

const now = Date.now()
const slots = [{
  key: SLOT, title: 'Fix payments retry + infra alarm', running: false,
  last_message: 'Edited the retry policy and the alarm threshold.', messages: 2,
  agent: 'kirocrew', memory_mode: 'persistent', project: WORKSPACE,
  modified: Math.floor(now / 1000), source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 2, queue: [], project: WORKSPACE,
  messages: [
    { role: 'user', ts: now / 1000 - 120, content: 'Bump the retry backoff in payments-service and raise the 5xx alarm threshold in payments-infra.' },
    { role: 'assistant', ts: now / 1000 - 30, content: 'Done: retry backoff is now exponential with jitter, and the 5xx alarm threshold is 2%. I also read the on-call runbook in team-docs.' },
  ],
}

const h = await openTranscriptHarness({ slot: SLOT, project: WORKSPACE, slots, detail, viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })

const file = (path, status, additions, deletions, staged = false) => ({ path, status, staged, additions, deletions })
const STATUS = {
  [SERVICE]: { repo: true, repoRoot: SERVICE, branch: 'fix/retry-backoff', ahead: 1, behind: 0, files: [
    file('src/payments/retry.py', 'M', 18, 6), file('test/test_retry.py', 'M', 24, 2), file('src/payments/jitter.py', '?', 31, 0),
  ] },
  [INFRA]: { repo: true, repoRoot: INFRA, branch: 'mainline', ahead: 0, behind: 0, files: [file('lib/alarms/five-xx.ts', 'M', 2, 2)] },
  [DOCS]: { repo: true, repoRoot: DOCS, branch: 'main', ahead: 0, behind: 0, files: [] },
}
const commit = (sha, message, mins, isHead = false) => ({ sha, message, author: 'Demo', date: new Date(now - mins * 60e3).toISOString(), isHead })
const LOG = {
  [SERVICE]: [commit('a41c9e2', 'retry: cap attempts at 5', 40, true), commit('7d02f1b', 'payments: add idempotency key', 600)],
  [INFRA]: [commit('c90e14a', 'alarms: split 4xx and 5xx', 90, true), commit('11fe3d8', 'pipeline: add gamma stage', 2000)],
  [DOCS]: [commit('5be7a10', 'runbook: payments on-call', 3000, true)],
}

// Mutable scene: which repos the repos route lists, whether that listing
// fails, and which repos' status route fails.
let repos = []
let listingFails = false
let statusFailsFor = new Set()
await h.page.route(/\/api\/dashboard\/config$/, route => json(route, { auto_open_git_panel: false }))
await h.page.route(/\/api\/project\/git/, async route => {
  const url = new URL(route.request().url())
  const p = url.searchParams.get('path')
  if (url.pathname === '/api/project/git/repos') {
    return listingFails
      ? json(route, { error: 'could not list this chat\'s repositories' }, 500)
      : json(route, { repos })
  }
  if (url.pathname === '/api/project/git/status') {
    return statusFailsFor.has(p)
      ? json(route, { error: 'git status failed' }, 500)
      : json(route, STATUS[p] ?? { repo: false, files: [] })
  }
  if (url.pathname === '/api/project/git/log') return json(route, { repo: !!LOG[p], commits: LOG[p] ?? [] })
  // GET /api/project/git (branch label): the workspace is not a repository.
  return json(route, STATUS[p] ? { path: p, repo: true, repoRoot: p, branch: STATUS[p].branch } : { path: p, repo: false })
})

/** One frame. `expected` is the section list; `notices` names the failure
 *  notices that must be visible: `listing` for the repos_failed ErrorNotice,
 *  `status` for the paths whose section must show its status ErrorNotice. */
async function shoot(name, expected, notices = {}) {
  await h.load('dark', { selector: 'textarea', settle: 800 })
  // Seed the side panel open on the Git tab, then reload so chatSlice
  // rehydrates it. An init script, registered AFTER load()'s storage-clearing
  // one, because init scripts re-run on reload (a page.evaluate seed is wiped).
  await h.page.addInitScript(slot => {
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
      tabs: [{ id: 'changes', kind: 'changes', title: 'Changes' }, { id: 'git', kind: 'git', title: 'Git' }],
      activeId: 'git',
    }))
  }, SLOT)
  await h.page.reload({ waitUntil: 'domcontentloaded' })
  try {
    await h.page.waitForSelector('[data-testid="git-repos-panel"]', { timeout: 20000 })
  } catch (e) {
    // Diagnostic frame only: named so it can never be mistaken for evidence.
    await h.page.screenshot({ path: join(OUT, `DEBUG-${name}-no-panel.png`) })
    throw e
  }
  await h.page.waitForTimeout(1500)
  // Each failing route is asked twice (retry: 1) before its notice renders, so
  // wait on the notice itself rather than trusting the settle window above.
  if (notices.listing) await h.page.getByTestId('git-repos-error').waitFor({ timeout: 10000 })
  for (const path of notices.status ?? []) {
    await h.page.locator(`[data-testid="git-repo-section"][data-path="${path}"]`)
      .getByTestId('git-panel-status-error').waitFor({ timeout: 10000 })
  }
  const rendered = await h.page.locator('[data-testid="git-repo-section"]').evaluateAll(els => els.map(e => ({
    path: e.getAttribute('data-path'),
    open: e.querySelector('[data-testid="git-repo-toggle"]')?.getAttribute('aria-expanded') === 'true',
    badge: e.querySelector('[data-testid="git-repo-toggle"]')?.innerText.includes('Project') ?? false,
    statusError: !!e.querySelector('[data-testid="git-panel-status-error"]'),
  })))
  const empty = await h.page.getByText('No Git repositories found for this chat yet.').count()
  const listingError = await h.page.getByTestId('git-repos-error').count()
  console.log(name, JSON.stringify({ rendered, empty, listingError }))
  if (JSON.stringify(rendered) !== JSON.stringify(expected)) throw new Error(`${name}: sections ${JSON.stringify(rendered)} != ${JSON.stringify(expected)}`)
  if (notices.listing) {
    if (listingError !== 1) throw new Error(`${name}: repos_failed notice missing`)
    if (!(await h.page.getByTestId('git-repos-error').isVisible())) throw new Error(`${name}: repos_failed notice not visible`)
    if (empty !== 0) throw new Error(`${name}: empty state shown beside the failure`)
  } else if (listingError !== 0) {
    throw new Error(`${name}: unexpected repos_failed notice`)
  }
  for (const path of notices.status ?? []) {
    const section = h.page.locator(`[data-testid="git-repo-section"][data-path="${path}"]`)
    if (!(await section.getByTestId('git-panel-status-error').isVisible())) throw new Error(`${name}: status notice for ${path} not visible`)
  }
  if (expected.length === 0 && !notices.listing && empty !== 1) throw new Error(`${name}: empty state missing`)
  await h.page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log('SHOT', join(OUT, `${name}.png`))
}

// 1. Workspace chat: three agent-touched repos, most recent first. The first
//    and the dirty ones open; the clean docs repo stays collapsed.
repos = [{ path: SERVICE, source: 'agent' }, { path: INFRA, source: 'agent' }, { path: DOCS, source: 'agent' }]
await shoot('1-workspace-chat', [
  { path: SERVICE, open: true, badge: false, statusError: false },
  { path: INFRA, open: true, badge: false, statusError: false },
  { path: DOCS, open: false, badge: false, statusError: false },
])

// 2. Project is a repository: listed first with the Project tag.
repos = [{ path: SERVICE, source: 'project' }, { path: INFRA, source: 'agent' }, { path: DOCS, source: 'agent' }]
await shoot('2-project-first', [
  { path: SERVICE, open: true, badge: true, statusError: false },
  { path: INFRA, open: true, badge: false, statusError: false },
  { path: DOCS, open: false, badge: false, statusError: false },
])

// 3. Nothing found yet.
repos = []
await shoot('3-empty', [])

// 4. The listing itself fails: the repos_failed ErrorNotice, no sections and
//    no empty state (a failure must not read as "nothing here").
listingFails = true
await shoot('4-listing-failed', [], { listing: true })
listingFails = false

// 5. Two repos; the second's status route fails. That section opens itself so
//    its status ErrorNotice is in view; the first is unaffected.
repos = [{ path: SERVICE, source: 'project' }, { path: INFRA, source: 'agent' }]
statusFailsFor = new Set([INFRA])
await shoot('5-status-failed-section', [
  { path: SERVICE, open: true, badge: true, statusError: false },
  { path: INFRA, open: true, badge: false, statusError: true },
], { status: [INFRA] })
statusFailsFor = new Set()

await h.close()
