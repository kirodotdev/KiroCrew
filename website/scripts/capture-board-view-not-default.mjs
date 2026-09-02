/**
 * Screenshot harness for the BOARD-VIEW DEFAULT.
 *
 * Board-vs-list is derived from a client-only localStorage flag AND the
 * server-side tag column list. Both scenarios below run against a gateway that
 * ALREADY HOLDS ONE COLUMN — the state that made the old default visible — and
 * differ only in what the client has stored:
 *
 *  1. default-list-view.png — no `mc-chat-config` at all (a new user, a second
 *     browser, a fresh profile). Renders the list. Under the old default of
 *     `true` this same client rendered the board, unasked.
 *
 *  2. explicit-opt-in-board-view.png — `tagColumnsEnabled: true` stored, i.e. a
 *     user who chose the board. Still renders the board, so the fix costs
 *     deliberate board users nothing.
 *
 * Both scenarios ASSERT as well as photograph: the run exits non-zero if the
 * fresh client renders a column strip, or if the opt-in client does not.
 *
 * Usage: node scripts/capture-board-view-not-default.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/board-view-not-default'

mkdirSync(OUT, { recursive: true })

const TAG_DONE = 'aaaaaaaaaaaa'
const COLUMN_ID = 'col-leftover'

const tags = [{ id: TAG_DONE, name: 'Done', color: '#22c55e', order: 0, status: true }]

// One unnamed match-all column — exactly what the sidebar's view toggle creates
// on its first activation, and what stays on the gateway after the toggle is
// switched back off.
const columns = [
  { id: COLUMN_ID, name: '', tag_ids: [], mode: 'any', order: 0, include_untagged: false },
]

const now = Math.floor(Date.now() / 1000)
const mkSlot = (key, title, slotTags) => ({
  key, title, running: false, last_message: '', messages: 2, agent: 'kirocrew',
  memory_mode: 'persistent', project: '', folder_id: '', modified: now,
  tags: slotTags, source_links: [], source_links_total: 0,
})

const slots = [
  mkSlot('chat-1', 'Trace the sidebar alignment regression', [TAG_DONE]),
  mkSlot('chat-2', 'Draft the release changelog', []),
  mkSlot('chat-3', 'Audit npm advisories before the nightly', []),
]

/**
 * Render /chat in a FRESH context so each scenario starts with empty storage,
 * then apply that scenario's stored config on top.
 * @param {import('playwright').Browser} browser
 * @param {string} base
 * @param {boolean} optIn whether the client has stored a board-view opt-in
 */
async function render(browser, base, optIn) {
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
  if (optIn) {
    await page.addInitScript(() => {
      const cfg = JSON.parse(localStorage.getItem('mc-chat-config') || '{}')
      cfg.tagColumnsEnabled = true
      localStorage.setItem('mc-chat-config', JSON.stringify(cfg))
    })
  }
  await page.goto(`${base}/chat`)
  await page.waitForSelector('[data-slot-key]', { timeout: 10000 })
  await page.waitForTimeout(500)
  return { context, page }
}

const stripCount = (page) => page.locator('[data-testid="column-strip"]').count()

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  // ── Scenario 1: fresh client, column on the gateway → list view ───────────
  {
    const { context, page } = await render(browser, base, false)
    const strips = await stripCount(page)
    if (strips !== 0) {
      throw new Error(`scenario 1: a client with no stored config rendered ${strips} column strip(s) — board view is still the default`)
    }
    const rows = await page.locator('[data-slot-key]').count()
    if (rows !== slots.length) {
      throw new Error(`scenario 1: expected the list to show all ${slots.length} sessions, saw ${rows}`)
    }
    await page.screenshot({ path: `${OUT}/default-list-view.png` })
    console.log('scenario 1 OK: no stored config → list view, all sessions in one lane')
    await context.close()
  }

  // ── Scenario 2: stored opt-in → board view still works ────────────────────
  {
    const { context, page } = await render(browser, base, true)
    await page.waitForSelector('[data-testid="column-strip"]', { timeout: 10000 })
    const column = await page.locator(`[data-testid="column-${COLUMN_ID}"]`).count()
    if (column !== 1) {
      throw new Error(`scenario 2: expected the opted-in client to render column ${COLUMN_ID}, saw ${column}`)
    }
    await page.screenshot({ path: `${OUT}/explicit-opt-in-board-view.png` })
    console.log('scenario 2 OK: stored tagColumnsEnabled=true → board view')
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
