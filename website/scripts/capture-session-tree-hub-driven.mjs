/**
 * Screenshot harness, and behaviour check, for the conductor lane over a HUB-DRIVEN
 * crew: a local lead executing on a connected crew, and the workers it opened there.
 *
 * Three frames over the same peer listing through the REAL `ChatSidebar` and the REAL
 * `useInstanceSessions`:
 *   before -- the workers arrive with no `parent`, which is what `_clean_peer_parent`
 *             sent before this change (their citation named the driven peer slot and
 *             was dropped whole): four `On worker-1` rows at the top level beside the
 *             local lead that opened them, each wearing the stray glyph of nobody.
 *   after  -- each worker arrives citing `parent: {slot, hub_key}` -- the LOCAL lead's
 *             key, as the hub now rewrites it: the lead is ONE row with its worker
 *             count and the subtree's badges, SHUT by default.
 *   opened -- one press on the lead's chevron, and every worker nests under it.
 *
 * The after frame is a fresh page load with `?hub=1`, not a payload swap on the
 * mounted page: swapping in place would read every null -> key transition as a
 * re-parent (`citedCreatorRef`) and auto-open the crew, photographing an adopt rather
 * than the collapsed default.
 *
 * Serves the capture page from the DEV server (`/capture/session-tree-hub-driven.html`),
 * with every other `/api/**` boot fixture answered by the shared stub.
 *
 * Usage: node scripts/capture-session-tree-hub-driven.mjs [devBase] [outDir]
 */
import { openSessionTreeHarness } from './lib/session-tree-harness.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6181'
const OUT = process.argv[3] || '../temp-screenshots/session-tree-hub-driven'
const LEAD = 'chat-2201'
const WORKERS = ['chat-2202', 'chat-2203', 'chat-2204', 'chat-2205']

const { page, check, rows, keys, rowOf, settleTheme, shot, finish } = await openSessionTreeHarness(OUT)
const orphanGlyphs = () => page.$$eval('[data-testid^="conductor-orphan-"]', els => els.length)

async function load(query) {
  await page.goto(`${BASE}/capture/session-tree-hub-driven.html?theme=dark${query}`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector(`[data-slot-key="${LEAD}"]`)
  await settleTheme()
  await page.waitForTimeout(400)
}

// ── before: the workers' citations were dropped at the hub ────────────────────
await load('')
console.log('before:', await keys())
check('before: every row is in the list', (await rows()).length === 6, `rows=${(await rows()).length}`)
check('before: the conductor lane is not offered -- nothing cites anyone, so no tree',
  !(await page.$('[data-testid="conductor-view-lane"]')))
check('before: the local lead has no chevron and counts nothing',
  !(await page.$(`[data-testid="conductor-child-count-${LEAD}"]`)))
await shot('before-hub-driven-workers-stray')

// ── after: each worker cites the local lead through hub_key ───────────────────
await load('&hub=1')
await page.waitForSelector(`[data-testid="conductor-child-count-${LEAD}"]`)
console.log('after: ', await keys())
const lead = await rowOf(LEAD)
check('after: the local lead is a top-level row', lead?.depth === '0', `depth=${lead?.depth}`)
for (const w of WORKERS) check(`after: ${w} is behind the chevron by default`, !(await rowOf(w)))
const count = await page.$eval(`[data-testid="conductor-child-count-${LEAD}"]`, el => el.textContent)
check('after: the shut lead counts its workers', count === String(WORKERS.length), `count=${count}`)
const needsYou = await page.$eval(`[data-testid="conductor-needs-you-${LEAD}"]`, el => el.textContent).catch(() => null)
check('after: the shut lead carries the worker awaiting approval', needsYou === '1', `needsYou=${needsYou}`)
const running = await page.$eval(`[data-testid="conductor-running-${LEAD}"]`, el => el.textContent).catch(() => null)
check('after: the shut lead carries the running worker', running === '1', `running=${running}`)
check('after: no orphan glyph', (await orphanGlyphs()) === 0)
await shot('after-local-lead-collapsed-by-default')

// ── opened: one press shows the peer workers under the LOCAL lead ─────────────
await page.click(`[data-testid="conductor-chevron-${LEAD}"]`)
await page.waitForSelector(`[data-slot-key="${WORKERS[0]}"]`)
await page.waitForTimeout(400)
console.log('opened:', await keys())
for (const w of WORKERS) {
  const r = await rowOf(w)
  check(`opened: ${w} nests one level under the local lead`, r?.depth === '1', `depth=${r?.depth}`)
}
check('opened: the count stays with the row', !!(await page.$(`[data-testid="conductor-child-count-${LEAD}"]`)))
check('opened: the aggregate leaves with the fold', !(await page.$(`[data-testid="conductor-needs-you-${LEAD}"]`)))
const control = await rowOf('chat-2188')
check('control: the unrelated local chat stays a top-level row', control?.depth === '0', `depth=${control?.depth}`)
await shot('after-local-lead-opened-nests-peer-workers')

await finish()
