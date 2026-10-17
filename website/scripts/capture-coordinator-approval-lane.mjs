/**
 * Screenshot harness for a SUB-AGENT (coordinator) approval lighting the parent
 * slot's Needs approval lane.
 *
 * Gateway-free, against the REAL built SPA (website/dist). The two ``slots``
 * frames it renders are NOT hand-written fixtures: each is the JSON that
 * ``DashboardState.serialize_slots()`` produced while a real
 * ``ApprovalCoordinator.request("spawn:…", slot="chat-parent")`` was pending --
 * one dumped from the base checkout, one from this branch (see the PR body for
 * the generator). The harness therefore shows what the frontend does with the
 * projection each backend actually emits, not what it would do with an ideal one.
 *
 *  1. lane-before.png -- base projection: the parent slot is parked on a spawn
 *     gate but the frame carries ``pending_approval: false``, so the card sits
 *     in Working and no lane, badge or glyph says a decision is owed.
 *  2. lane-after.png  -- this branch: the same pending approval, the frame now
 *     carries ``pending_approval: true`` + ``pending_approval_info`` from the
 *     coordinator record, and the card moves into Needs approval.
 *
 * Both frames ASSERT lane placement before shooting; a wrong lane exits non-zero.
 *
 * Usage: node scripts/capture-coordinator-approval-lane.mjs <before.json> <after.json> [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const [beforePath, afterPath, outArg] = process.argv.slice(2)
if (!beforePath || !afterPath) {
  console.error('usage: capture-coordinator-approval-lane.mjs <before.json> <after.json> [outDir]')
  process.exit(2)
}
const OUT = outArg || '../temp-screenshots/coordinator-approval-lane'
mkdirSync(OUT, { recursive: true })

const LANES = ['needs_approval', 'waiting', 'working', 'idle']
const laneColumns = LANES.map((key, i) => ({
  id: `lane-${key}`, name: '', tag_ids: [], mode: 'any', order: i,
  include_untagged: false, source: 'state', state_key: key,
}))

const loadFrame = (path) => {
  const frame = JSON.parse(readFileSync(path, 'utf8'))
  // ``modified`` is a sidebar sort key the wire frame does not carry.
  const now = Math.floor(Date.now() / 1000)
  return frame.slots.map((s, i) => ({ ...s, modified: now - i * 60 }))
}

async function keysIn(page, columnId) {
  return page.evaluate((cid) => {
    const col = document.querySelector(`[data-testid="column-${cid}"]`)
    if (!col) return null
    return Array.from(col.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))
  }, columnId)
}

async function renderBoard(browser, base, slots) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 } })
  const page = await context.newPage()
  logPageProblems(page)
  const cfg = JSON.stringify({ tagColumnsEnabled: true })
  await stubDashboardApi(page, {
    slots,
    localStorageEntries: { 'mc-chat-config': cfg, 'mc-sidebar-width': '760' },
    extra: async (path, route) => {
      if (path === '/api/chat/tags') { await json(route, []); return true }
      if (path === '/api/chat/tag-columns') { await json(route, laneColumns); return true }
      return false
    },
  })
  await page.goto(`${base}/chat`)
  await page.waitForSelector('[data-testid="column-strip"]', { timeout: 10000 })
  await page.waitForTimeout(600)
  const dialogs = await page.locator('[role="dialog"]').count()
  if (dialogs) throw new Error(`unexpected dialog open (${dialogs}) -- the frame would be covered`)
  return { context, page }
}

async function shoot(browser, base, slots, name, expectParentLane) {
  const { context, page } = await renderBoard(browser, base, slots)
  const got = await keysIn(page, `lane-${expectParentLane}`)
  if (!got || !got.includes('chat-parent')) {
    throw new Error(`${name}: expected chat-parent in lane-${expectParentLane}, saw ${JSON.stringify(got)}`)
  }
  for (const lane of LANES) {
    if (lane === expectParentLane) continue
    const other = await keysIn(page, `lane-${lane}`)
    if (other && other.includes('chat-parent')) {
      throw new Error(`${name}: chat-parent also in lane-${lane}`)
    }
  }
  const sibling = await keysIn(page, 'lane-idle')
  if (!sibling || !sibling.includes('chat-sibling')) {
    throw new Error(`${name}: chat-sibling should stay in Idle, saw ${JSON.stringify(sibling)}`)
  }
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log(`${name} OK: chat-parent in ${expectParentLane}, chat-sibling in idle`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await shoot(browser, base, loadFrame(beforePath), 'lane-before', 'working')
    await shoot(browser, base, loadFrame(afterPath), 'lane-after', 'needs_approval')
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
