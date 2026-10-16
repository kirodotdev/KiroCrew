/**
 * Screenshot harness for the Git tab listing every repository a chat works in.
 *
 * Runs the REAL built SPA (website/dist) with /api/** answered from fixtures.
 * The side panel is seeded open on the Git tab, so no auto-open is involved
 * (that path only fires for a project that is itself a repository).
 *
 * Nine frames:
 *   workspace   project = the shared workspace (not a repo); three repos the
 *               agent touched, two dirty -> three sections, dirty ones open
 *   project     project IS a repo: it comes first and wears the Project tag
 *   empty       nothing found -> "No Git repositories to show. One appears here
 *               when the agent edits files or runs commands in it." The chat is
 *               a question answered with no tool call, so the agent has touched
 *               no repository and the empty list is coherent (a transcript
 *               reporting edits would read the empty list as a fault)
 *   listing     GET /api/project/git/repos fails on the FIRST load -> the
 *               repos_failed ErrorNotice with its Retry beside the agent
 *               hand-off, no sections and no empty state
 *   status      two repos; the second's status route fails -> that section
 *               opens itself with its status ErrorNotice in the body
 *   older       the response carries `omitted: 3` -> "The agent worked in 27
 *               repositories; the 24 most recent are listed. Each hidden one
 *               reappears once the agent works in it again." under the list
 *   loading     the repos request is held in flight -> spinner and
 *               "Loading this chat's repositories..."
 *   refresh     the list has loaded (the workspace frame's three repos), then
 *               a background refresh of GET /api/project/git/repos fails -> the
 *               list stays mounted with the repos_refresh_failed ErrorNotice
 *               ("Couldn't refresh this chat's repositories. The list below may
 *               be out of date.") and its Retry ABOVE it (React Query keeps the
 *               last good listing)
 *   incomplete  the response carries `incomplete: true` (an executed tool call
 *               named more paths than the per-call bound keeps) -> the muted
 *               repos_incomplete line under the list
 *
 * Each frame asserts the rendered section paths (and, for the notice frames,
 * the notice and its copy) before it is written.
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
    { role: 'assistant', ts: now / 1000 - 30, content: 'Done: retry backoff is now exponential with jitter, and the 5xx alarm threshold is 2%. I also ran the runbook link check in team-docs.' },
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

// Mutable scene: which repos the repos route lists, how many less recently used
// ones the server's cap left out (`omitted`, a wire field GET
// /api/project/git/repos always carries), whether that listing
// fails or is still in flight, and which repos' status route fails.
let repos = []
let omitted = 0
// The agent-touched repositories actually listed (`listed` on the wire). Null
// means every row is one the agent worked in, which holds for every frame's
// fixture: the transcript has the agent edit both payments repos and run a
// check in team-docs.
let listed = null
let incomplete = false
const LIMIT = 24
let listingFails = false
let listingPending = false
let statusFailsFor = new Set()
await h.page.route(/\/api\/dashboard\/config$/, route => json(route, { auto_open_git_panel: false }))
await h.page.route(/\/api\/project\/git/, async route => {
  const url = new URL(route.request().url())
  const p = url.searchParams.get('path')
  if (url.pathname === '/api/project/git/repos') {
    // Held open, never fulfilled: the tab stays on its first-listing state
    // until the next load() navigates away and drops the request.
    if (listingPending) return
    return listingFails
      ? json(route, { error: 'could not list this chat\'s repositories' }, 500)
      : json(route, { repos, omitted, listed: listed ?? repos.length, incomplete })
  }
  if (url.pathname === '/api/project/git/status') {
    return statusFailsFor.has(p)
      // The real route's outage body (file_api/git_panel.py), verbatim: a
      // stub that invents its own error text shows reviewers copy the product
      // cannot produce.
      ? json(route, { error: "Couldn't read the repository status.", code: 'git_status_unavailable' }, 503)
      : json(route, STATUS[p] ?? { repo: false, files: [] })
  }
  if (url.pathname === '/api/project/git/log') return json(route, { repo: !!LOG[p], commits: LOG[p] ?? [] })
  // GET /api/project/git (branch label): the workspace is not a repository.
  return json(route, STATUS[p] ? { path: p, repo: true, repoRoot: p, branch: STATUS[p].branch } : { path: p, repo: false })
})

/** One frame. `expected` is the section list; `notices` names the failure
 *  notices that must be visible: `listing` for the repos_failed ErrorNotice,
 *  `status` for the paths whose section must show its status ErrorNotice,
 *  `omitted` for the exact text of the capacity line under the list, and
 *  `loading` for the first-listing spinner (the listing is still in flight).
 *  `refreshFail` loads the listing successfully, then makes the NEXT background
 *  refresh of /api/project/git/repos fail: React Query keeps the last good
 *  listing, so the sections stay mounted with the repos_refresh_failed notice
 *  above them (pair it with `listing: true` to assert that notice). */
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
  if (notices.refreshFail) {
    // The first listing loaded; wait for its sections, then make the next
    // background refresh fail and drive React Query to run it. Both
    // refetchOnWindowFocus and the 10s refetchInterval reach the repos query,
    // so a focus event triggers it immediately and the interval is the backstop.
    await h.page.locator('[data-testid="git-repo-section"]').first().waitFor({ timeout: 10000 })
    listingFails = true
    await h.page.evaluate(() => {
      window.dispatchEvent(new Event('focus'))
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await h.page.getByTestId('git-repos-error').waitFor({ timeout: 15000 })
  }
  // Each failing route is asked twice (retry: 1) before its notice renders, so
  // wait on the notice itself rather than trusting the settle window above.
  if (notices.listing) await h.page.getByTestId('git-repos-error').waitFor({ timeout: 10000 })
  if (notices.loading) await h.page.getByTestId('git-repos-loading').waitFor({ timeout: 10000 })
  if (notices.omitted) await h.page.getByTestId('git-repos-omitted').waitFor({ timeout: 10000 })
  if (notices.incomplete) await h.page.getByTestId('git-repos-incomplete').waitFor({ timeout: 10000 })
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
  const empty = await h.page.getByText('No Git repositories to show. One appears here when the agent edits files or runs commands in it.').count()
  const listingError = await h.page.getByTestId('git-repos-error').count()
  const loading = await h.page.getByTestId('git-repos-loading').count()
  const omittedLine = await h.page.getByTestId('git-repos-omitted').count()
  const incompleteLine = await h.page.getByTestId('git-repos-incomplete').count()
  console.log(name, JSON.stringify({ rendered, empty, listingError, loading, omittedLine }))
  if (JSON.stringify(rendered) !== JSON.stringify(expected)) throw new Error(`${name}: sections ${JSON.stringify(rendered)} != ${JSON.stringify(expected)}`)
  if (notices.listing) {
    if (listingError !== 1) throw new Error(`${name}: ${notices.refreshFail ? 'repos_refresh_failed' : 'repos_failed'} notice missing`)
    if (!(await h.page.getByTestId('git-repos-error').isVisible())) throw new Error(`${name}: failure notice not visible`)
    // A first-load failure says it couldn't LIST; a background-refresh failure
    // over a list that is still shown says it couldn't REFRESH and that the
    // list below may be stale -- never "couldn't list" while repos are visible.
    const expectedCopy = notices.refreshFail
      ? "Couldn't refresh this chat's repositories. The list below may be out of date."
      : "Couldn't list this chat's repositories."
    const noticeText = await h.page.getByTestId('git-repos-error').innerText()
    if (!noticeText.includes(expectedCopy)) throw new Error(`${name}: notice copy is ${JSON.stringify(noticeText)}, expected to include ${JSON.stringify(expectedCopy)}`)
    if (notices.refreshFail && noticeText.includes("Couldn't list this chat's repositories.")) throw new Error(`${name}: refresh failure still shows the first-load "couldn't list" copy`)
    if (!(await h.page.getByTestId('git-repos-retry').isVisible())) throw new Error(`${name}: Retry missing from the failure notice`)
    if (empty !== 0) throw new Error(`${name}: empty state shown beside the failure`)
  } else if (listingError !== 0) {
    throw new Error(`${name}: unexpected repos_failed notice`)
  }
  if (notices.loading) {
    const spinner = h.page.getByTestId('git-repos-loading')
    if (loading !== 1 || !(await spinner.isVisible())) throw new Error(`${name}: loading state not visible`)
    if ((await spinner.innerText()) !== "Loading this chat's repositories...") throw new Error(`${name}: loading copy is ${JSON.stringify(await spinner.innerText())}`)
    if (empty !== 0) throw new Error(`${name}: empty state shown while the listing is in flight`)
  } else if (loading !== 0) {
    throw new Error(`${name}: loading state still showing`)
  }
  if (notices.omitted) {
    const line = h.page.getByTestId('git-repos-omitted')
    if (omittedLine !== 1 || !(await line.isVisible())) throw new Error(`${name}: omitted notice not visible`)
    if ((await line.innerText()) !== notices.omitted) throw new Error(`${name}: omitted copy is ${JSON.stringify(await line.innerText())}`)
  } else if (omittedLine !== 0) {
    throw new Error(`${name}: unexpected omitted notice`)
  }
  if (notices.incomplete) {
    const line = h.page.getByTestId('git-repos-incomplete')
    if (incompleteLine !== 1 || !(await line.isVisible())) throw new Error(`${name}: incomplete notice not visible`)
    if ((await line.innerText()) !== notices.incomplete) throw new Error(`${name}: incomplete copy is ${JSON.stringify(await line.innerText())}`)
  } else if (incompleteLine !== 0) {
    throw new Error(`${name}: unexpected incomplete notice`)
  }
  for (const path of notices.status ?? []) {
    const section = h.page.locator(`[data-testid="git-repo-section"][data-path="${path}"]`)
    if (!(await section.getByTestId('git-panel-status-error').isVisible())) throw new Error(`${name}: status notice for ${path} not visible`)
  }
  if (expected.length === 0 && !notices.listing && !notices.loading && empty !== 1) throw new Error(`${name}: empty state missing`)
  if (notices.scrollToEnd) {
    await h.page.getByTestId('git-repos-panel').evaluate(el => { el.scrollTop = el.scrollHeight })
    await h.page.waitForTimeout(300)
    if (!(await h.page.getByTestId('git-repos-omitted').isVisible())) throw new Error(`${name}: omitted notice not in view after scrolling`)
  }
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

// 3. Nothing found. The transcript must show NO agent work: an empty list
//    means the agent has edited files or run commands in no repository, so a
//    transcript reporting edits to payments-service would read the empty list
//    as a fault. Override this frame's transcript with a question answered
//    with no tool call — the agent has touched no repository, so the empty
//    list is coherent.
repos = []
const emptyFrameMessages = detail.messages
detail.messages = [
  { role: 'user', ts: now / 1000 - 120, content: 'Quick question before I assign anything: when does exponential backoff beat a fixed retry delay?' },
  { role: 'assistant', ts: now / 1000 - 90, content: 'Exponential backoff grows the wait after each failed attempt, usually with jitter, so a struggling downstream is not hit by every client in lockstep. A fixed delay is simpler and fine when failures are rare and independent, but it can synchronize retries into waves.' },
]
await shoot('3-empty', [])
detail.messages = emptyFrameMessages

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

// 6. The server capped the list: it holds exactly LIMIT repositories (the
//    server reports `omitted` only once the list is full, so a shorter list
//    with a count would be a state the product cannot produce), three less
//    recently used ones are not listed, and the line under the list states the
//    total and how many are listed, as a rule, not an action. The panel is
//    scrolled to the end so the line is in the frame.
const FILLER = Array.from({ length: LIMIT - 3 }, (_, i) => `/home/user/workspace/service-${String(i + 1).padStart(2, '0')}`)
for (const p of FILLER) STATUS[p] = { repo: true, repoRoot: p, branch: 'main', ahead: 0, behind: 0, files: [] }
repos = [{ path: SERVICE, source: 'project' }, { path: INFRA, source: 'agent' }, { path: DOCS, source: 'agent' },
  ...FILLER.map(path => ({ path, source: 'agent' }))]
omitted = 3
// The project row is one the agent worked in (it edited the retry policy), so
// all LIMIT rows count: the line reads LIMIT + 3 in all, LIMIT listed.
listed = LIMIT
await shoot('6-older-hidden', [
  { path: SERVICE, open: true, badge: true, statusError: false },
  { path: INFRA, open: true, badge: false, statusError: false },
  { path: DOCS, open: false, badge: false, statusError: false },
  ...FILLER.map(path => ({ path, open: false, badge: false, statusError: false })),
], { omitted: `The agent worked in ${LIMIT + 3} repositories; the ${LIMIT} most recent are listed. Each hidden one reappears once the agent works in it again.`, scrollToEnd: true })
omitted = 0
listed = null

// 7. The first listing is still in flight: spinner and wait copy, never the
//    empty state (which would claim "nothing here" before anything was read).
listingPending = true
await shoot('7-loading', [], { loading: true })
listingPending = false

// 8. The list has loaded (the workspace frame's three repos), then a background
//    refresh of the repos route fails. React Query keeps the last good listing,
//    so the sections stay mounted with the repos_refresh_failed notice and its
//    Retry ABOVE them -- a gateway restart mid-session must not blank the tab
//    the user is reading, and the notice must not say "couldn't list" while the
//    repositories are visible right below it.
repos = [{ path: SERVICE, source: 'agent' }, { path: INFRA, source: 'agent' }, { path: DOCS, source: 'agent' }]
listingFails = false
await shoot('8-refresh-failed', [
  { path: SERVICE, open: true, badge: false, statusError: false },
  { path: INFRA, open: true, badge: false, statusError: false },
  { path: DOCS, open: false, badge: false, statusError: false },
], { listing: true, refreshFail: true })
listingFails = false

// 9. An executed tool call named more paths than the per-call bound keeps, so
//    the server marks the list incomplete: the muted line under the list says
//    a repository may be missing rather than the list silently passing for
//    whole (the a-bound-bounds-every-field-it-retains rule).
repos = [{ path: SERVICE, source: 'agent' }, { path: INFRA, source: 'agent' }, { path: DOCS, source: 'agent' }]
incomplete = true
await shoot('9-incomplete', [
  { path: SERVICE, open: true, badge: false, statusError: false },
  { path: INFRA, open: true, badge: false, statusError: false },
  { path: DOCS, open: false, badge: false, statusError: false },
], { incomplete: 'Some repositories the agent worked in may not be listed. Each one appears here once the agent works in it again.' })
incomplete = false

await h.close()
