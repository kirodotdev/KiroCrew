/**
 * Screenshot harness for #33210: sentences glued from catalog pieces around a
 * styled name.
 *
 * Shoots three surfaces, where the old catalog pieces read in English word order
 * around the styled words in Japanese:
 *
 *  1. `devfleet`: the Dev Fleet how-to paragraph (bold Pull + Build, Pod, Rebase,
 *     Prune).
 *  2. `issue`: an Issue Radar issue whose timeline holds a title change.
 *  3. `pull`: an Issue Radar pull request whose timeline holds a title change.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures.
 *
 * Usage: npm run build && node scripts/capture-33210-styled-name-sentences.mjs <outDir> <before|after> [distDir] [lang]
 *   Run `before` against a build of origin/main (pass its dist as distDir).
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/33210-styled-name-sentences'
const LABEL = process.argv[3] === 'before' ? 'before' : 'after'
const DIST = process.argv[4] || DEFAULT_DIST
const LANG = process.argv[5] || 'ja'
mkdirSync(OUT, { recursive: true })

// The catalog of the build being shot, so a `before` run reads origin/main's keys.
const catalog = JSON.parse(readFileSync(join(DIST, '..', 'src', 'i18n', 'locales', `${LANG}.json`), 'utf8'))
const tr = key => key.split('.').reduce((node, part) => node?.[part], catalog)

const now = Math.floor(Date.now() / 1000)
const FLEET = {
  base_branch: 'main', gateway_service_active: true, manual_restart: 'kirocrew restart',
  live_state_known: true, staged_target: null, staged_cancel_available: false,
  worktrees: [
    { name: 'main', is_main: true, is_live: true, is_staged: false, running: false, has_dist: true, behind: 0, branch: 'main', path: '/w/main', last_updated_at: now - 900 },
    { name: 'kirocrew-wt-widgets', is_main: false, is_live: false, is_staged: false, running: true, port: 7780, health: 200, has_dist: true, behind: 2, branch: 'feat/widgets', path: '/w/kirocrew-wt-widgets', last_updated_at: now - 3600 },
  ],
}

const REPO = { owner: 'kirodotdev', repo: 'Kiro', provider: 'github', host: 'github.com' }
const ISO = '2026-08-01T00:00:00Z'
const RENAMED = {
  kind: 'renamed', actor: 'alice', created_at: '2026-08-04T00:00:00Z',
  rename: { from: 'Crash on startup', to: 'Crash on startup when config is missing' },
}
const ISSUES = {
  ...REPO, state: 'open', from_cache: true,
  issues: [{ number: 1, title: RENAMED.rename.to, url: '#', labels: ['bug'], comments: 0, updated_at: ISO, created_at: ISO, state: 'open', author: 'alice', assignees: [] }],
}
const ISSUE = {
  ...REPO, number: 1, from_cache: true, timeline: [RENAMED],
  detail: {
    number: 1, title: RENAMED.rename.to, body: 'Repro steps included.', state: 'open', state_reason: null, url: '#',
    author: 'alice', author_association: 'MEMBER', created_at: ISO, updated_at: ISO, closed_at: null, closed_by: null,
    comments: 0, locked: false, labels: [{ name: 'bug', color: 'd73a4a', description: '' }], assignees: [], milestone: null, reactions: null,
  },
}
const PR_TITLE = { from: 'Dark mode toggle', to: 'Wire up the dark-mode toggle' }
const PULL_ROW = {
  number: 42, title: PR_TITLE.to, url: '#', state: 'open', draft: false, labels: ['enhancement'], author: 'bob',
  updated_at: ISO, created_at: ISO, merged_at: null, assignees: [], requested_reviewers: [], base: 'main', head: 'dark-mode',
  head_sha: 'abc1234', additions: 120, deletions: 8, changed_files: 4,
  checks_state: 'success', checks_counts: { failure: 0, running: 0, success: 5, other: 0 }, mergeable: true, mergeable_state: 'clean',
}
const PULL = {
  ...REPO, number: 42, from_cache: true, checks: [],
  timeline: [{ kind: 'renamed', actor: 'bob', created_at: '2026-08-04T00:00:00Z', rename: PR_TITLE }],
  detail: {
    ...PULL_ROW, body: 'Adds the toggle and persists the choice.', merged: false, author_association: 'CONTRIBUTOR', closed_at: null,
    merged_by: null, comments: 0, review_comments: 0, commits: 1, labels: [{ name: 'enhancement', color: 'a2eeef', description: '' }],
    milestone: null, auto_merge: null,
  },
}
const IR = '/api/apps/issue-radar'

const extra = async (path, route) => {
  if (path === '/api/members') return json(route, { members: [], default_agent: 'kirocrew' }), true
  if (path === '/api/apps') {
    return json(route, [
      { name: 'issue-radar', displayName: 'Issue Radar', enabled: true, builtin: true, version: '0.1.0', ui: { pages: [{ route: '/issue-radar', label: 'Issue Radar', icon: 'Radar' }] } },
      { name: 'dev-fleet', displayName: 'Dev Fleet', enabled: true, builtin: true, version: '0.1.0', ui: { pages: [{ route: '/dev-fleet', label: 'Dev Fleet', icon: 'Server' }] } },
    ]), true
  }
  if (path === '/apps/dev-fleet/api/fleet') return json(route, FLEET), true
  if (path === '/apps/dev-fleet/api/disk') return json(route, { total_mb: 2048 }), true
  if (path.startsWith('/apps/dev-fleet/api/')) return json(route, {}), true
  if (path === `${IR}/repos`) return json(route, { repos: [{ ...REPO, enabled: true, permissions: { push: true, triage: true } }] }), true
  if (path === `${IR}/me`) return json(route, { login: 'owner', provider: 'github', host: 'github.com' }), true
  if (path === `${IR}/labels`) return json(route, { ...REPO, from_cache: true, labels: [{ name: 'bug', color: 'd73a4a', description: '' }, { name: 'enhancement', color: 'a2eeef', description: '' }] }), true
  if (path === `${IR}/members`) return json(route, { ...REPO, members: [{ login: 'alice', role: 'admin' }], source: 'collaborators', from_cache: true }), true
  if (path === `${IR}/settings`) {
    return json(route, { ...REPO, settings: { triage_labels: [], unlabeled_is_untriaged: true, good_first_issue_labels: [], notify_on_new_issue: false, revision: 1 } }), true
  }
  if (path === `${IR}/issues`) return json(route, ISSUES), true
  if (path === `${IR}/issue`) return json(route, ISSUE), true
  if (path === `${IR}/pulls`) return json(route, { ...REPO, state: 'open', from_cache: true, bulk_max: 50, pulls: [PULL_ROW] }), true
  if (path === `${IR}/pulls/search`) return json(route, { ...REPO, state: 'open', from_cache: true, bulk_max: 50, pulls: [] }), true
  if (path === `${IR}/pull`) return json(route, PULL), true
  if (path === `${IR}/pull/runs`) return json(route, { ...REPO, number: 42, runs: [] }), true
  if (path === `${IR}/pull-ai` || path === `${IR}/issue-ai`) return json(route, { ...REPO, number: 1, summary: '', suggested_labels: [], from_cache: true }), true
  if (path === `${IR}/recent-repos`) return json(route, { repos: [] }), true
  if (path === `${IR}/investigation`) return json(route, { ...REPO, record: null }), true
  if (path === `${IR}/deps`) return json(route, { schema: 1, edges: [], nodes: {} }), true
  if (path.startsWith(`${IR}/`)) return json(route, {}), true
  return false
}

let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()

const open = async (url, theme, uiState) => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const localStorageEntries = { 'mc-lang': LANG, 'mc-privacy-notice-v1': '1', 'mc-onboarded': '1' }
  if (uiState) {
    localStorageEntries['kc:issue-radar:active-repo'] = JSON.stringify({ owner: REPO.owner, repo: REPO.repo })
    localStorageEntries['kc:issue-radar:ui-state'] = JSON.stringify(uiState)
  }
  await stubDashboardApi(page, { extra, theme, localStorageEntries })
  logPageProblems(page)
  await page.goto(`${base}${url}`, { waitUntil: 'domcontentloaded' })
  return { context, page }
}

/** Check the page shows the new whole sentence (after) or still the old fragment (before). */
const expectText = async (page, name, probe) => {
  const text = (await page.locator('body').innerText()).replaceAll('\n', ' ')
  if (!probe || !text.includes(probe)) fail(`${name}: expected ${JSON.stringify(probe)} on the page`)
}
const firstPlain = value => value.split('<')[0].replace(/\s+/g, ' ').trim()

for (const theme of ['light', 'dark']) {
  // 1. Dev Fleet how-to paragraph
  {
    const { context, page } = await open('/dev-fleet', theme)
    await page.getByText('kirocrew-wt-widgets').first().waitFor({ timeout: 30000 }).catch(() => fail('devfleet: rows did not render'))
    await page.waitForTimeout(800)
    const how = LABEL === 'after'
      ? firstPlain(tr('pages.devFleetPage.row_actions_how_to'))
      : firstPlain(tr('pages.devFleetPage.each_row_below_is_a_git_worktree_discovered_from'))
    await expectText(page, 'devfleet', how)
    await page.screenshot({ path: `${OUT}/01-devfleet-${LANG}-${LABEL}-${theme}.png`, clip: { x: 0, y: 0, width: 1280, height: 420 } })
    console.log(`wrote 01-devfleet-${LANG}-${LABEL}-${theme}.png`)
    await context.close()
  }
  // 2. Issue timeline title change, 3. pull-request timeline title change
  for (const [n, kind, uiState, title] of [
    ['02', 'issue', { mainView: 'issues', stateFilter: 'open', selectedIssue: 1 }, RENAMED.rename.from],
    ['03', 'pull', { mainView: 'pulls', prStateFilter: 'open', selectedPull: 42, prSelectedPull: 42 }, PR_TITLE.from],
  ]) {
    const { context, page } = await open('/issue-radar', theme, uiState)
    const old = page.locator('.line-through', { hasText: title }).first()
    await old.waitFor({ timeout: 30000 }).catch(() => fail(`${kind}: title change did not render`))
    await old.scrollIntoViewIfNeeded().catch(() => {})
    await page.waitForTimeout(800)
    const row = old.locator('xpath=ancestor::*[contains(@class,"flex")][1]')
    await page.screenshot({ path: `${OUT}/${n}-${kind}-${LANG}-${LABEL}-${theme}.png` })
    const line = (await old.locator('xpath=..').innerText().catch(() => '')).replace(/\s+/g, ' ')
    console.log(`wrote ${n}-${kind}-${LANG}-${LABEL}-${theme}.png  line: ${line}`)
    if (!(await row.count())) fail(`${kind}: no row around the title change`)
    await context.close()
  }
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
