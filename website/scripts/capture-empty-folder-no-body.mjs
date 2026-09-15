/**
 * Screenshot probe: a folder with nothing in it has no body.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free - no kiro-cli, no live backend). The fixture is the shape that
 * motivated the change: one working subfolder plus thirteen empty sibling
 * subfolders, each of which used to render its own "New chat in <name>" row and
 * push the one real session off the top of the list.
 *
 * Frames written, per run:
 *   <prefix>-01-list    the sidebar tree
 *   <prefix>-02-board   the same fixture in board (tag-columns) view
 *
 * The point is the delta, so run it against this branch (after) and against
 * origin/main (before):
 *   node scripts/capture-empty-folder-no-body.mjs ../temp-screenshots/empty-folder-no-body after
 *
 * Usage: node scripts/capture-empty-folder-no-body.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/empty-folder-no-body'
const PREFIX = process.argv[3] || 'after'
// Third argument selects the state: the feature is opt-in, so `after` frames seed
// the setting and `before` frames leave it at its default.
const OPT_IN = (process.argv[4] || (PREFIX === 'after' ? 'on' : 'off')) === 'on'

mkdirSync(OUT, { recursive: true })

const ROOT = 'pipeline'
const EMPTY_NAMES = [
  'agents', 'cron', 'security', 'core', 'apps', 'skills', 'packaging',
  'channels', 'gateway', 'area-cron', 'area-agents', 'area-core', 'area-tests',
]

const folders = [
  { id: ROOT, name: 'pipeline-issue-fix', order: 0, collapsed: false },
  { id: 'dashboard', name: 'dashboard', order: 0, collapsed: false, parent_id: ROOT },
  ...EMPTY_NAMES.map((name, i) => ({ id: name, name, order: i + 1, collapsed: false, parent_id: ROOT })),
]

// Exactly one real session, filed in the only subfolder that holds work. Its tag
// keeps it in a board column, so the board frame is not an empty board.
const TAG = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
const slots = [{
  key: 's1', title: 'Worker - #7627 Settings Display toggle for user', messages: 12,
  running: false, agent: 'kirocrew', created: '2026-09-07T01:00:00Z',
  last_ts: '2026-09-07T21:14:00Z', folder_id: 'dashboard', tags: [TAG],
}]
const tags = [{ id: TAG, name: 'Working', color: '#1a1', order: 0, status: true }]
const columns = [{ id: 'col-working', name: 'Working', tag_ids: [TAG], mode: 'any', order: 0 }]

async function newPage(context, { board = false, optIn = false } = {}) {
  const page = await context.newPage()
  // The stub clears localStorage in its own init script, and Playwright does not
  // order separately registered init scripts, so the board flag has to ride
  // `localStorageEntries` rather than an addInitScript of our own.
  await stubDashboardApi(page, {
    folders, slots,
    // Tag routes go through `extra`, the same way the six other board-view
    // capture scripts serve them - the stub needs no new options for this.
    extra: async (path, route) => {
      if (path === '/api/chat/tags') { await json(route, tags); return true }
      if (path === '/api/chat/tag-columns') { await json(route, columns); return true }
      return false
    },
    // The feature is opt-in, so the frames that show it must seed the setting the
    // Settings toggle writes. Seeded through `localStorageEntries` because the
    // stub clears localStorage in its own init script.
    localStorageEntries: (board || optIn)
      ? { 'mc-chat-config': JSON.stringify({ ...(board ? { tagColumnsEnabled: true } : {}), ...(optIn ? { hideEmptyFolderBody: true } : {}) }) }
      : null,
  })
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  return page
}

let base

async function main() {
  const served = await serveDist()
  base = served.base
  const browser = await chromium.launch()
  const shared = { viewport: { width: 1400, height: 1250 }, deviceScaleFactor: 2 }

  // List view: the tree, cropped to the session/folder panel.
  const listContext = await browser.newContext(shared)
  const list = await newPage(listContext, { optIn: OPT_IN })
  const anchor = list.locator(`[data-testid="folder-collapse-${ROOT}"]`)
  const box = (await anchor.count()) ? await anchor.first().boundingBox() : null
  const lx = box ? Math.max(0, box.x - 44) : 470
  await list.screenshot({
    path: `${OUT}/${PREFIX}-01-list.png`,
    clip: { x: lx, y: 118, width: Math.min(1400 - lx, 380), height: 1000 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-01-list.png`)
  await listContext.close()

  // Board view: the column rows carry a column-scoped test id, not the list
  // view's `folder-collapse-<id>`, so the crop anchors on that instead.
  const boardContext = await browser.newContext(shared)
  const board = await newPage(boardContext, { board: true, optIn: OPT_IN })
  // Clip to the COLUMN element rather than a fixed width from the row: a fixed
  // width ran past the column's right edge and caught the page's empty-state
  // artwork, which reads as unidentifiable UI in a review frame.
  const column = board.locator(`[data-testid="column-${columns[0].id}"]`)
  const colBox = (await column.count()) ? await column.first().boundingBox() : null
  const boardRow = board.locator(`[data-testid="col-${columns[0].id}-folder-${ROOT}"]`)
  const boardBox = (await boardRow.count()) ? await boardRow.first().boundingBox() : null
  const bx = colBox ? Math.max(0, colBox.x - 8) : boardBox ? Math.max(0, boardBox.x - 24) : 470
  const bw = colBox ? Math.min(colBox.width + 16, 1400 - bx) : Math.min(1400 - bx, 420)
  await board.screenshot({
    path: `${OUT}/${PREFIX}-02-board.png`,
    clip: { x: bx, y: 96, width: bw, height: 940 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-02-board.png`)
  await boardContext.close()

  await browser.close()
  served.srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
