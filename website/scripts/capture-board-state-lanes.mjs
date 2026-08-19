/**
 * Screenshot harness for the BOARD STATE LANES.
 *
 * Two scenarios against the REAL built SPA (website/dist), gateway-free:
 *
 *  1. board-before.png — the board a user actually gets today: one unnamed
 *     match-all column holding every session in one pile, which is the reported
 *     "weird flow". The Planned/ToDo/Implementation/Review/Done status
 *     vocabulary exists but nothing ever applies it to a session, so a board
 *     built from it never moves.
 *
 * A sub-agent's OWED approval also lands its parent in Needs approval, but that
 * count arrives over the WebSocket rather than on the slot payload, so it is
 * covered by src/test/sessionLane.test.ts instead of here.
 *
 *  2. board-after.png — the same sessions on the four derived lanes. Each card
 *     sits in the lane its own runtime state puts it in, and the lane counts add
 *     up to the session total because a lane assignment is exclusive.
 *
 * Both scenarios ASSERT as well as photograph: the run exits non-zero if a
 * session lands in the wrong lane, in more than one lane, or in none.
 *
 * Usage: node scripts/capture-board-state-lanes.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/board-state-lanes'

mkdirSync(OUT, { recursive: true })

// The shipped seed vocabulary: five status tags nothing ever assigns.
const tags = [
  { id: 'planned', name: 'Planned', color: '#6b7280', order: 0, status: true },
  { id: 'todo', name: 'ToDo', color: '#3b82f6', order: 1, status: true },
  { id: 'implementation', name: 'Implementation', color: '#8b5cf6', order: 2, status: true },
  { id: 'review', name: 'Review', color: '#f59e0b', order: 3, status: true },
  { id: 'done', name: 'Done', color: '#10b981', order: 4, status: true },
]

const now = Math.floor(Date.now() / 1000)
const mkSlot = (key, title, state) => ({
  key, title, running: false, last_message: '', messages: 6, agent: 'kirocrew',
  memory_mode: 'persistent', project: '', folder_id: '', modified: now,
  tags: [], source_links: [], source_links_total: 0,
  ...state,
})

// One session per distinguishable runtime state, plus the pairs that prove
// precedence (an approval owed by a session whose turn is still "running").
const slots = [
  mkSlot('chat-approval', 'Deploy staging bundle', { running: true, pending_approval: true, last_message: 'Run terraform apply?' }),
  mkSlot('chat-subagents', 'Audit dependency tree', { subagents_running: true, last_message: '4 sub-agents fanned out' }),
  mkSlot('chat-question', 'Migrate auth provider', { running: true, needs_input: true, last_message: 'Which tenant should I target?' }),
  mkSlot('chat-options', 'Draft release notes', { has_options: true, options: ['Ship it', 'Hold'], last_message: 'Two options for the changelog' }),
  mkSlot('chat-interrupted', 'Fix flaky shard 4', { interrupted: true, last_message: 'turn ended without a reply' }),
  mkSlot('chat-working', 'Refactor config loader', { running: true, last_message: 'editing loader.py' }),
  mkSlot('chat-orchestrating', 'Ship board lanes', { running: true, orchestrating: true, last_message: 'stage 2 of 4' }),
  mkSlot('chat-queued', 'Weekly dependency sweep', { queue_depth: 2, last_message: 'two prompts queued' }),
  mkSlot('chat-idle-1', 'Weekly report draft', { last_message: 'done — summary posted' }),
  mkSlot('chat-idle-2', 'Investigate disk alert', { last_message: 'closed, no action needed' }),
]

const LANES = ['needs_approval', 'waiting', 'working', 'idle']
const laneColumns = LANES.map((key, i) => ({
  id: `lane-${key}`, name: '', tag_ids: [], mode: 'any', order: i,
  include_untagged: false, source: 'state', state_key: key,
}))

// The placeholder the view toggle used to create: unnamed, unfiltered, match-all.
const placeholderColumns = [
  { id: 'col-placeholder', name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false },
]

const EXPECTED = {
  'lane-needs_approval': ['chat-approval'],
  'lane-waiting': ['chat-question', 'chat-options', 'chat-interrupted'],
  'lane-working': ['chat-working', 'chat-orchestrating', 'chat-queued', 'chat-subagents'],
  'lane-idle': ['chat-idle-1', 'chat-idle-2'],
}

async function keysIn(page, columnId) {
  return page.evaluate((cid) => {
    const col = document.querySelector(`[data-testid="column-${cid}"]`)
    if (!col) return null
    return Array.from(col.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))
  }, columnId)
}

async function renderBoard(browser, base, columns, sidebarWidth = 0) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 } })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    extra: async (path, route) => {
      if (path === '/api/chat/tags') { await json(route, tags); return true }
      if (path === '/api/chat/tag-columns') { await json(route, columns); return true }
      return false
    },
  })
  // Registered after the stub's own init script, which clears storage first.
  await page.addInitScript((width) => {
    const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
    cfg.tagColumnsEnabled = true
    localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    // Seeding lanes widens the sidebar to fit them (boardSidebarWidth); store the
    // same width so the frame shows what a real user gets, not a clipped strip.
    if (width) localStorage.setItem('mc-sidebar-width', String(width))
  }, sidebarWidth)
  await page.goto(`${base}/chat`)
  await page.waitForSelector('[data-testid="column-strip"]', { timeout: 10000 })
  await page.waitForTimeout(600)
  return { context, page }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  // ── Scenario 1: the board as shipped — one unnamed pile ──────────────────
  {
    const { context, page } = await renderBoard(browser, base, placeholderColumns)
    const pile = await keysIn(page, 'col-placeholder')
    if (!pile || pile.length !== slots.length) {
      throw new Error(`scenario 1: expected the placeholder column to hold all ${slots.length} sessions, saw ${JSON.stringify(pile)}`)
    }
    await page.screenshot({ path: `${OUT}/board-before.png` })
    console.log(`scenario 1 OK: one unnamed column holding all ${pile.length} sessions`)
    await context.close()
  }

  // ── Scenario 2: the four derived lanes ──────────────────────────────────
  {
    const { context, page } = await renderBoard(browser, base, laneColumns, 760)
    const placed = []
    for (const [columnId, expected] of Object.entries(EXPECTED)) {
      const got = await keysIn(page, columnId)
      if (!got) throw new Error(`scenario 2: lane ${columnId} did not render`)
      const missing = expected.filter(k => !got.includes(k))
      const extra = got.filter(k => !expected.includes(k))
      if (missing.length || extra.length) {
        throw new Error(
          `scenario 2: ${columnId} membership wrong — missing ${JSON.stringify(missing)}, unexpected ${JSON.stringify(extra)}`,
        )
      }
      placed.push(...got)
    }
    // Exhaustive and exclusive: every session placed exactly once. A card in no
    // lane vanishes from the board; a card in two is counted twice.
    const dupes = placed.filter((k, i) => placed.indexOf(k) !== i)
    if (dupes.length) throw new Error(`scenario 2: sessions in more than one lane: ${JSON.stringify(dupes)}`)
    const unplaced = slots.map(s => s.key).filter(k => !placed.includes(k))
    if (unplaced.length) throw new Error(`scenario 2: sessions in no lane at all: ${JSON.stringify(unplaced)}`)
    await page.screenshot({ path: `${OUT}/board-after.png` })
    console.log(`scenario 2 OK: ${placed.length} sessions across ${LANES.length} lanes, each in exactly one`)
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
