/**
 * Screenshot harness for the BOARD COLUMN TAG FILTER (issue #1897).
 *
 * Two scenarios against the REAL built SPA (website/dist), gateway-free:
 *
 *  1. column-unfiltered.png — a board with an unfiltered column (tag_ids [])
 *     next to a tag-filtered column that has not been applied yet. The
 *     unfiltered lane shows every session (the match-all state).
 *
 *  2. column-filtered.png — the same board where the "Jira" column carries
 *     tag_ids [jira]. Only the two Jira-tagged sessions render in that lane;
 *     the unfiltered lane still shows all five.
 *
 * Both scenarios ASSERT as well as photograph: the run exits non-zero if the
 * filtered lane shows a session without the tag (the reported bug: every
 * column matching the ALL SESSIONS count) or hides one that carries it.
 *
 * Usage: node scripts/capture-board-column-tag-filter.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/board-column-tag-filter'

mkdirSync(OUT, { recursive: true })

const TAG_JIRA = 'aaaaaaaaaaaa'
const TAG_ALERT = 'bbbbbbbbbbbb'
const COL_JIRA = 'col-jira'
const COL_ALL = 'col-all'

const tags = [
  { id: TAG_JIRA, name: 'Jira', color: '#2563eb', order: 0, status: false },
  { id: TAG_ALERT, name: 'Alert', color: '#dc2626', order: 1, status: false },
]

const now = Math.floor(Date.now() / 1000)
const mkSlot = (key, title, slotTags) => ({
  key, title, running: false, last_message: '', messages: 2, agent: 'kirocrew',
  memory_mode: 'persistent', project: '', folder_id: '', modified: now,
  tags: slotTags, source_links: [], source_links_total: 0,
})

const slots = [
  mkSlot('chat-jira-1', 'Fix JIRA-4321 login redirect', [TAG_JIRA]),
  mkSlot('chat-jira-2', 'Triage JIRA-8800 flaky suite', [TAG_JIRA]),
  mkSlot('chat-alert-1', 'Investigate disk alert', [TAG_ALERT]),
  mkSlot('chat-plain-1', 'Weekly report draft', []),
  mkSlot('chat-plain-2', 'Refactor config loader', []),
]

const columnsFor = (jiraTagIds) => [
  { id: COL_JIRA, name: 'Jira', tag_ids: jiraTagIds, mode: 'any', order: 0, include_untagged: false },
  { id: COL_ALL, name: '', tag_ids: [], mode: 'any', order: 1, include_untagged: false },
]

async function keysIn(page, columnId) {
  return page.evaluate((cid) => {
    const col = document.querySelector(`[data-testid="column-${cid}"]`)
    if (!col) return null
    return Array.from(col.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))
  }, columnId)
}

async function renderBoard(page, base, jiraTagIds) {
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path === '/api/chat/tags') { await json(route, tags); return true }
      if (path === '/api/chat/tag-columns') { await json(route, columnsFor(jiraTagIds)); return true }
      return false
    },
  })
  await page.goto(`${base}/chat`)
  await page.waitForSelector('[data-testid="column-strip"]', { timeout: 10000 })
  await page.waitForTimeout(500)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 } })
  const page = await context.newPage()
  logPageProblems(page)

  // ── Scenario 1: unfiltered column shows every session (match-all) ────────
  await renderBoard(page, base, [])
  const before = await keysIn(page, COL_JIRA)
  if (!before || before.length !== slots.length) {
    throw new Error(`scenario 1: expected the unfiltered lane to show all ${slots.length} sessions, saw ${JSON.stringify(before)}`)
  }
  await page.screenshot({ path: `${OUT}/column-unfiltered.png` })
  console.log('scenario 1 OK: unfiltered column shows all sessions')

  // ── Scenario 2: tag-filtered column shows ONLY matching sessions ─────────
  await renderBoard(page, base, [TAG_JIRA])
  const filtered = await keysIn(page, COL_JIRA)
  const expected = ['chat-jira-1', 'chat-jira-2']
  if (!filtered || filtered.length !== expected.length || !expected.every(k => filtered.includes(k))) {
    throw new Error(`scenario 2: expected filtered lane to show ${JSON.stringify(expected)}, saw ${JSON.stringify(filtered)} — the reported bug (column matching ALL SESSIONS)`)
  }
  const allLane = await keysIn(page, COL_ALL)
  if (!allLane || allLane.length !== slots.length) {
    throw new Error(`scenario 2: expected the match-all lane to keep all ${slots.length} sessions, saw ${JSON.stringify(allLane)}`)
  }
  await page.screenshot({ path: `${OUT}/column-filtered.png` })
  console.log('scenario 2 OK: filtered column shows only the tagged sessions')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
