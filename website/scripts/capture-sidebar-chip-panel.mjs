/**
 * Screenshot harness for "a session-list PR / issue chip opens in the side
 * panel", not in a browser tab.
 *
 * The change is a click TARGET, so a still of the chip proves nothing: the chip
 * looks identical before and after. The evidence has to be the sequence — the
 * sidebar with the panel closed, then the panel open on the very pull request
 * the chip names, from one click, without leaving the app. This harness drives
 * that click for real and fails loudly if the panel does not arrive.
 *
 * It also covers the case a still could never show: the ISSUE chip on chat-b
 * names an issue only the USER pasted, which the panel's own transcript scan
 * deliberately excludes (a user-referenced link is a Resource, not a Change).
 * Revealing it is what puts it in the panel at all.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures (gateway-free — no
 * kiro-cli, no dashboard token, no provider CLI). Only the network and the
 * localStorage seed are stubbed; the client code under test is unmodified.
 *
 * Usage: node scripts/capture-sidebar-chip-panel.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-chip-panel'
const ACTIVE = 'chat-a'
const OTHER = 'chat-b'
const BUSY = 'chat-c'
const REPO = 'https://github.com/kirodotdev/KiroCrew'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/634'
const ISSUE_URL = 'https://github.com/kirodotdev/KiroCrew/issues/701'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const slots = [
  {
    key: ACTIVE, title: 'Draft the release notes', running: false, messages: 4,
    agent: 'kirocrew', modified: now, last_ts: '2026-08-07T00:10:00Z', folder_id: '',
    last_message: 'Grouped the entries by area.',
  },
  {
    key: OTHER, title: 'Session flyout for the collapsed sidebar', running: false, messages: 6,
    agent: 'kirocrew', modified: now - 900, last_ts: '2026-08-07T00:00:00Z', folder_id: '',
    last_message: 'All checks pass; waiting on review.',
    source_links: [
      { provider: 'github', number: 634, url: PR_URL, state: 'open', ci: 'passed', kind: 'change' },
      { provider: 'github', number: 701, url: ISSUE_URL, kind: 'issue' },
    ],
    source_links_total: 2,
  },
  {
    // A long-running session that mentioned five pull requests. The payload is
    // ordered newest-first (see `_pr_source_links`), so the chip budget now shows
    // the three most recent and collapses the OLDEST two into "+2" -- previously
    // this row showed #612/#613/#614 and hid the one being worked on.
    key: BUSY, title: 'Sweep the native selects', running: false, messages: 24,
    agent: 'kirocrew', modified: now - 1800, last_ts: '2026-08-06T23:30:00Z', folder_id: '',
    last_message: 'Rebased and pushed; 47 checks green.',
    source_links: [
      { provider: 'github', number: 648, url: `${REPO}/pull/648`, state: 'open', ci: 'running', kind: 'change' },
      { provider: 'github', number: 641, url: `${REPO}/pull/641`, state: 'open', ci: 'passed', kind: 'change' },
      { provider: 'github', number: 620, url: `${REPO}/pull/620`, state: 'merged', kind: 'change' },
    ],
    source_links_total: 5,
  },
]

/** chat-b's transcript. The PR is AGENT-surfaced; the issue is USER-pasted. */
const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: `Pick up ${ISSUE_URL} next.`, ts: now - 1800 },
    { role: 'assistant', content: `Opened ${PR_URL} with the flyout behind a hover-intent delay.`, ts: now - 900 },
  ],
}

const pullRequest = {
  provider: 'github', url: PR_URL, number: 634,
  title: 'feat(chat): session hover flyout for the collapsed sidebar',
  description: 'Hovering the collapsed sidebar toggle opens a compact session list.',
  state: 'OPEN', draft: false, mergedAt: '', updatedAt: new Date().toISOString(),
  headBranch: 'feat/session-hover-flyout', baseBranch: 'main', headSha: 'a79a20f7',
  author: 'diwu', additions: 2235, deletions: 72, changedFiles: 32,
  mergeable: 'mergeable', mergeStateStatus: 'blocked',
  commits: [{
    sha: 'a79a20f7', message: 'feat(chat): session hover flyout for the collapsed sidebar',
    author: 'diwu', committedAt: new Date().toISOString(), url: PR_URL,
  }],
  checks: [
    { name: 'Frontend Tests', workflow: 'CI', status: 'COMPLETED', conclusion: 'SUCCESS', bucket: 'passed', url: '', startedAt: '', completedAt: '' },
    { name: 'Backend Tests', workflow: 'CI', status: 'COMPLETED', conclusion: 'SUCCESS', bucket: 'passed', url: '', startedAt: '', completedAt: '' },
    { name: 'Type check', workflow: 'CI', status: 'COMPLETED', conclusion: 'SUCCESS', bucket: 'passed', url: '', startedAt: '', completedAt: '' },
  ],
  comments: [],
  files: [
    { path: 'website/src/pages/chat/SessionFlyout.tsx', status: 'added', additions: 318, deletions: 0, patch: '' },
    { path: 'website/src/hooks/useHoverIntent.ts', status: 'added', additions: 96, deletions: 0, patch: '' },
    { path: 'website/src/pages/ChatSidebar.tsx', status: 'modified', additions: 41, deletions: 12, patch: '' },
  ],
}

const issue = {
  provider: 'github', url: ISSUE_URL, number: 701,
  title: 'Collapsed sidebar hides which session is running',
  description: 'With the sidebar collapsed there is no way to see a running session without expanding it.',
  state: 'open', stateReason: '', author: 'diwu',
  createdAt: '2026-07-30T09:00:00Z', updatedAt: new Date().toISOString(),
  closedAt: '', closedBy: '',
  labels: [{ name: 'enhancement', color: 'a2eeef', description: '' }],
  assignees: ['diwu'], milestone: null, commentCount: 1, locked: false, reactions: null,
  comments: [{
    id: 'c1', author: 'diwu', body: 'A hover flyout over the toggle would cover this.',
    createdAt: '2026-07-31T10:00:00Z', url: `${ISSUE_URL}#issuecomment-1`,
  }],
  linkedChanges: [{ provider: 'github', url: PR_URL, number: 634, title: pullRequest.title, state: 'OPEN' }],
}

/**
 * Routes this harness owns on top of the shared boot fixtures.
 *
 * Each branch returns an explicit `true`: the stub treats a falsy return as "not
 * handled" and fulfils the route itself, and `json()` resolves to undefined, so
 * `return json(...)` alone double-fulfils ("Route is already handled!").
 */
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  if (path === '/api/source/pull-request') return json(route, pullRequest), true
  if (path === '/api/source/pull-request/checks') return json(route, { checks: pullRequest.checks }), true
  if (path === '/api/source/pull-request/status') {
    return json(route, { statuses: { [PR_URL]: { state: 'open', ci: 'passed' } }, refreshing: [], ttlSecs: 60 }), true
  }
  if (path === '/api/source/issue') return json(route, issue), true
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    // The story starts at a 10px chip glyph, illegible in a 1x full-window shot.
    deviceScaleFactor: 2,
  })
  // A chip that fell back to plain link navigation would open a TAB instead of
  // the panel — and silently, since the harness would still screenshot a valid
  // page. Collect them so the assertion below can say so out loud. Bound per
  // page as `popup`, not as the context's `page` event: the latter also fires
  // for the harness's own newPage() calls.
  const popups = []

  let page = null
  /**
   * A FRESH page per theme. stubDashboardApi installs one `**\/api\/**` handler
   * and bakes the theme into /api/theme/boot, so calling it twice on one page
   * throws "Route is already handled!" and the theme could not change anyway.
   */
  async function load(theme) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    page.on('popup', p => popups.push(`${theme}: ${p.url()}`))
    await stubDashboardApi(page, { slots, theme, extra })
    // Registered after the shared stub, which clears localStorage in its own
    // init script.
    await page.addInitScript(slot => {
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-privacy-notice-v1', '1')
      localStorage.setItem('mc-sidebar-pinned', 'true')
    }, ACTIVE)
    await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  for (const theme of ['dark', 'light']) {
    await load(theme)

    const prChip = page.locator(`a[title^="Open ${PR_URL} in the side panel"]`)
    await prChip.first().waitFor({ state: 'visible', timeout: 15000 })

    // 1. Before: the chips sit on the other session's row, panel closed.
    await page.screenshot({ path: `${OUT}/01-chips-${theme}.png` })
    const row = await prChip.first().evaluate(el => {
      const r = el.closest('.session-row').getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height }
    })
    await page.screenshot({
      path: `${OUT}/02-chips-crop-${theme}.png`,
      clip: { x: Math.max(0, row.x - 8), y: Math.max(0, row.y - 60), width: row.width + 16, height: row.height + 80 },
    })

    // 2. One click on the PR chip: switch to chat-b AND open Changes on #634.
    await prChip.first().click()
    await page.getByText(pullRequest.title, { exact: false }).first()
      .waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(800)
    await page.screenshot({ path: `${OUT}/03-pr-in-panel-${theme}.png` })

    // 3. The issue chip opens the Issues tab on #701 — a link the panel's own
    //    transcript scan excludes, so only the reveal can put it there.
    await page.locator(`a[title^="Open ${ISSUE_URL} in the side panel"]`).first().click()
    await page.getByText(issue.title, { exact: false }).first()
      .waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(800)
    await page.screenshot({ path: `${OUT}/04-issue-in-panel-${theme}.png` })

    // The five-pull-request row: newest first, oldest two behind "+2". This is
    // what the reviewer asked for -- the chip you are working on is no longer the
    // one that gets collapsed.
    const busyRow = await page.locator('.session-row', { hasText: 'Sweep the native selects' }).first()
      .evaluate(el => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height } })
    await page.screenshot({
      path: `${OUT}/06-chip-order-${theme}.png`,
      clip: { x: Math.max(0, busyRow.x - 8), y: Math.max(0, busyRow.y - 8), width: busyRow.width + 16, height: busyRow.height + 16 },
    })
    const chipOrder = await page.locator('.session-row', { hasText: 'Sweep the native selects' }).first()
      .locator('a[title^="Open "]').evaluateAll(els => els.map(e => e.textContent.trim()))
    if (chipOrder.join(',') !== '#648,#641,#620') {
      throw new Error(`chips are not newest-first: ${JSON.stringify(chipOrder)}`)
    }
    console.log(`${theme}: PR + issue reached the panel; chip order ${chipOrder.join(' ')} + overflow`)
  }

  // 4. Keyboard parity, in a real browser rather than by reasoning about the
  //    spec. The row's own onKeyDown bails when the event target is not the row,
  //    so Enter on a FOCUSED CHIP falls to the anchor's default activation — and
  //    whether that reveals in-panel or leaves for the provider depends on
  //    whether the browser's default action for Enter is "fire a click" (which
  //    the chip's own handler then intercepts). jsdom cannot answer that; this
  //    can. If it ever leaves for the provider, the popup assertion below fails.
  await load('dark')
  const kbChip = page.locator(`a[title^="Open ${PR_URL} in the side panel"]`)
  await kbChip.first().waitFor({ state: 'visible', timeout: 15000 })
  await kbChip.first().focus()
  const focused = await page.evaluate(() => document.activeElement?.getAttribute('title') ?? '')
  // Prefix, not equality: the tooltip also carries the modifier hint, and the
  // claim being checked is only that focus landed on THIS chip.
  if (!focused.startsWith(`Open ${PR_URL} in the side panel`)) {
    throw new Error(`chip did not take focus (activeElement title: ${focused})`)
  }
  await page.keyboard.press('Enter')
  await page.getByText(pullRequest.title, { exact: false }).first()
    .waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/05-pr-via-keyboard-dark.png` })
  console.log('keyboard: Enter on a focused chip reached the panel')

  if (popups.length) {
    throw new Error(`chip activation opened ${popups.length} browser tab(s): ${popups.join(', ')}`)
  }
  console.log('no browser tab was opened by any chip activation')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
