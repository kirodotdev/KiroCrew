/**
 * Screenshot harness for FLAT VIEW INSIDE THE BOARD.
 *
 * Two scenarios against the REAL built SPA (website/dist), gateway-free:
 *
 *  1. board-folders.png — the board with flat view OFF: every column renders
 *     the folder blocks (all root folders as drop targets) with matching
 *     sessions nested under them.
 *
 *  2. board-flat.png — the same board with flat view ON: the board keeps
 *     rendering (it is no longer replaced by the single flat lane), folder
 *     blocks disappear from every column, each matching session sits directly
 *     in its lane in the tree's sort order, and a column with no matching
 *     sessions says "No sessions" even though folders exist.
 *
 * Both scenarios ASSERT as well as photograph: the run exits non-zero when a
 * column's membership, folder-header presence, or the empty-lane notice does
 * not match the contract (mirrors src/test/ChatSidebar.flatBoard.test.tsx).
 *
 * Usage: node scripts/capture-flat-board.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/flat-board'

mkdirSync(OUT, { recursive: true })

const tags = [
  { id: 'todo', name: 'ToDo', color: '#3b82f6', order: 0, status: true },
  { id: 'impl', name: 'Implementation', color: '#8b5cf6', order: 1, status: true },
  { id: 'review', name: 'Review', color: '#f59e0b', order: 2, status: true },
  { id: 'blocked', name: 'Blocked', color: '#ef4444', order: 3, status: false },
]

const folders = [
  { id: 'f-alpha', name: 'Alpha', order: 0 },
  { id: 'f-beta', name: 'Beta', order: 1 },
]

const now = Math.floor(Date.now() / 1000)
const mkSlot = (key, title, extra) => ({
  key, title, running: false, last_message: '', messages: 6, agent: 'kirocrew',
  memory_mode: 'persistent', project: '', folder_id: '', modified: now,
  tags: [], source_links: [], source_links_total: 0,
  ...extra,
})

// Sessions spread across both folders plus unfoldered ones, with distinct
// recency so the flat ordering inside a lane is visibly the tree's sort.
const slots = [
  mkSlot('chat-triage', 'Triage inbox', { tags: ['todo'], pinned: true, modified: now - 3600, last_message: 'pinned, unfoldered' }),
  mkSlot('chat-plan', 'Plan sessions page', { tags: ['todo'], folder_id: 'f-alpha', modified: now - 60, last_message: 'in Alpha' }),
  mkSlot('chat-spec', 'Spec flat board', { tags: ['todo'], folder_id: 'f-beta', modified: now - 600, last_message: 'in Beta' }),
  mkSlot('chat-port', 'Port claim matcher', { tags: ['impl'], folder_id: 'f-alpha', modified: now - 120, last_message: 'in Alpha' }),
  mkSlot('chat-wire', 'Wire flat lanes', { tags: ['impl'], folder_id: 'f-beta', modified: now - 30, last_message: 'in Beta' }),
  mkSlot('chat-cr', 'Review CR-123', { tags: ['review'], folder_id: 'f-alpha', modified: now - 300, last_message: 'in Alpha' }),
]

const columns = [
  { id: 'col-todo', name: 'ToDo', tag_ids: ['todo'], mode: 'any', order: 0, include_untagged: false },
  { id: 'col-impl', name: 'Implementation', tag_ids: ['impl'], mode: 'any', order: 1, include_untagged: false },
  { id: 'col-review', name: 'Review', tag_ids: ['review'], mode: 'any', order: 2, include_untagged: false },
  // No session carries 'blocked': in flat view this lane must say "No sessions".
  { id: 'col-blocked', name: 'Blocked', tag_ids: ['blocked'], mode: 'any', order: 3, include_untagged: false },
]

const MEMBERSHIP = {
  'col-todo': ['chat-triage', 'chat-plan', 'chat-spec'],
  'col-impl': ['chat-port', 'chat-wire'],
  'col-review': ['chat-cr'],
  'col-blocked': [],
}

// Flat ordering inside a lane: pinned first, then date-desc (the tree's sort).
const FLAT_ORDER = {
  'col-todo': ['chat-triage', 'chat-plan', 'chat-spec'],
  'col-impl': ['chat-wire', 'chat-port'],
  'col-review': ['chat-cr'],
  'col-blocked': [],
}

async function columnInfo(page, columnId) {
  return page.evaluate((cid) => {
    const col = document.querySelector(`[data-testid="column-${cid}"]`)
    if (!col) return null
    return {
      keys: Array.from(col.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key')),
      // Folder blocks are the elements carrying data-folder-drop (the folder
      // header's drop target) — a structural probe, immune to session text.
      folderBlocks: col.querySelectorAll('[data-folder-drop]').length,
      saysNoSessions: col.textContent.includes('No sessions'),
    }
  }, columnId)
}

async function renderBoard(browser, base, { flat }) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 } })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    folders,
    localStorageEntries: {
      'mc-chat-config': JSON.stringify({ tagColumnsEnabled: true }),
      'mc-sidebar-flat-view': flat ? '1' : '0',
      // Four 220px lanes + gaps + padding: keep the whole strip in frame.
      'mc-sidebar-width': '940',
    },
    extra: async (path, route) => {
      if (path === '/api/chat/tags') { await json(route, tags); return true }
      if (path === '/api/chat/tag-columns') { await json(route, columns); return true }
      return false
    },
  })
  await page.goto(`${base}/chat`)
  await page.waitForSelector('[data-testid="column-strip"]', { timeout: 10000 })
  await page.waitForTimeout(600)
  return { context, page }
}

function assertMembership(scenario, colId, got, expected) {
  const missing = expected.filter(k => !got.includes(k))
  const extra = got.filter(k => !expected.includes(k))
  if (missing.length || extra.length) {
    throw new Error(`${scenario}: ${colId} membership wrong — missing ${JSON.stringify(missing)}, unexpected ${JSON.stringify(extra)}`)
  }
}

async function main() {
  const { srv, base } = await serveDist()
  // chromiumSandbox:false — AL2023 hosts cannot run Chromium's sandbox.
  const browser = await chromium.launch({ chromiumSandbox: false })

  // ── Scenario 1: flat view OFF — folder blocks inside every column ────────
  {
    const { context, page } = await renderBoard(browser, base, { flat: false })
    for (const [colId, expected] of Object.entries(MEMBERSHIP)) {
      const info = await columnInfo(page, colId)
      if (!info) throw new Error(`scenario 1: ${colId} did not render`)
      assertMembership('scenario 1', colId, info.keys, expected)
      // Every column shows ALL root folders as drop targets, matching or not.
      if (info.folderBlocks < 2) {
        throw new Error(`scenario 1: ${colId} is missing its folder blocks (saw ${info.folderBlocks}, expected 2)`)
      }
      if (info.saysNoSessions) throw new Error(`scenario 1: ${colId} shows the empty notice despite folder blocks`)
    }
    await page.screenshot({ path: `${OUT}/board-folders.png` })
    console.log('scenario 1 OK: folder blocks render inside every column')
    await context.close()
  }

  // ── Scenario 2: flat view ON — flat rows in lanes, board still wins ──────
  {
    const { context, page } = await renderBoard(browser, base, { flat: true })
    const flatLane = await page.$('[data-testid="flat-view-lane"]')
    if (flatLane) throw new Error('scenario 2: the single flat lane rendered — the board should win the layout')
    for (const [colId, expected] of Object.entries(FLAT_ORDER)) {
      const info = await columnInfo(page, colId)
      if (!info) throw new Error(`scenario 2: ${colId} did not render`)
      if (JSON.stringify(info.keys) !== JSON.stringify(expected)) {
        throw new Error(`scenario 2: ${colId} rows/order wrong — expected ${JSON.stringify(expected)}, saw ${JSON.stringify(info.keys)}`)
      }
      if (info.folderBlocks !== 0) {
        throw new Error(`scenario 2: ${colId} still renders ${info.folderBlocks} folder block(s) in flat view`)
      }
      const shouldBeEmpty = expected.length === 0
      if (info.saysNoSessions !== shouldBeEmpty) {
        throw new Error(`scenario 2: ${colId} empty-notice mismatch (saysNoSessions=${info.saysNoSessions}, expected ${shouldBeEmpty})`)
      }
    }
    await page.screenshot({ path: `${OUT}/board-flat.png` })
    console.log('scenario 2 OK: flat rows inside lanes, no folder blocks, empty lane says "No sessions"')
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
