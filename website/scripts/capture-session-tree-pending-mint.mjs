/**
 * Screenshot harness, and behaviour check, for the conductor lane over a
 * JUST-DISPATCHED crew: workers a lead minted that have not had a turn yet.
 *
 * Four frames over the same four workers through the REAL `ChatSidebar`:
 *   before -- the payload resolves no parent for a worker with no crew log, which is
 *             what the sidebar received before this change: four top-level strays
 *             beside the lead that minted them, each wearing the glyph of nobody.
 *   after  -- the payload answers from the row's own mint witness: the lead is ONE
 *             row with its worker count, SHUT by default.
 *   opened -- one press on the lead's chevron, and all four nest under it.
 *   ran    -- the workers have had their first turn, so each has a crew log and the
 *             fold answers for it instead. The rows do not MOVE: same parent, same
 *             depth, same order, now with their turn counts and running state.
 *
 * Each frame is a fresh page load with its own query string, not a payload swap on the
 * mounted page: in the product a worker either has a node or it does not, so the two
 * answers never meet in one session, and swapping them in place would read every
 * null -> key transition as a re-parent (`citedCreatorRef`) and auto-open the crew,
 * photographing an adopt rather than the collapsed default.
 *
 * Serves the capture page from the DEV server (`/capture/session-tree-pending-mint.html`),
 * with every `/api/**` boot fixture answered by the shared stub.
 *
 * Usage: node scripts/capture-session-tree-pending-mint.mjs [devBase] [outDir]
 */
import { openSessionTreeHarness } from './lib/session-tree-harness.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6181'
const OUT = process.argv[3] || '../temp-screenshots/session-tree-pending-mint'
const LEAD = 'chat-2481'
const WORKERS = ['chat-2486', 'chat-2487', 'chat-2488', 'chat-2489']
const UNRELATED = 'chat-2475'

const { page, check, rows, keys, rowOf, settleTheme, shot, finish } = await openSessionTreeHarness(OUT)
const orphanGlyphs = () => page.$$eval('[data-testid^="conductor-orphan-"]', els => els.length)

async function load(query) {
  await page.goto(`${BASE}/capture/session-tree-pending-mint.html?theme=dark${query}`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector(`[data-slot-key="${LEAD}"]`)
  await settleTheme()
  await page.waitForTimeout(400)
}

// ── before: a worker with no crew log gets no parent ─────────────────────────
await load('')
console.log('before:', await keys())
check('before: every row is in the list', (await rows()).length === 6, `rows=${(await rows()).length}`)
check('before: the conductor lane is not offered -- no row cites anyone, so there is no tree',
  !(await page.$('[data-testid="conductor-view-lane"]')))
for (const w of WORKERS) {
  const r = await rowOf(w)
  // No lane means no nesting wrapper at all, so the row carries no depth: the list is
  // flat and every worker sits beside the lead that minted it.
  check(`before: ${w} is listed flat, with no nesting`, !!r && r.depth === null, `depth=${r?.depth}`)
}
check('before: the lead counts nothing',
  !(await page.$(`[data-testid="conductor-child-count-${LEAD}"]`)))
await shot('before-dispatched-workers-stray')

// ── after: the mint witness answers, which is this change ────────────────────
await load('&minted=1')
await page.waitForSelector(`[data-testid="conductor-child-count-${LEAD}"]`)
console.log('after: ', await keys())
const lead = await rowOf(LEAD)
check('after: the lead is a top-level row', lead?.depth === '0', `depth=${lead?.depth}`)
for (const w of WORKERS) check(`after: ${w} is behind the chevron by default`, !(await rowOf(w)))
const count = await page.$eval(`[data-testid="conductor-child-count-${LEAD}"]`, el => el.textContent)
check('after: the shut lead counts its workers', count === String(WORKERS.length), `count=${count}`)
check('after: no orphan glyph -- nobody is nested under a session that is gone',
  (await orphanGlyphs()) === 0)
await shot('after-mint-witness-nests-collapsed')

// ── opened: one press shows all four under the lead ──────────────────────────
await page.click(`[data-testid="conductor-chevron-${LEAD}"]`)
await page.waitForSelector(`[data-slot-key="${WORKERS[0]}"]`)
await page.waitForTimeout(400)
console.log('opened:', await keys())
const openedDepths = []
for (const w of WORKERS) {
  const r = await rowOf(w)
  openedDepths.push(r?.depth)
  check(`opened: ${w} nests one level under the lead`, r?.depth === '1', `depth=${r?.depth}`)
}
check('opened: the count stays with the row',
  !!(await page.$(`[data-testid="conductor-child-count-${LEAD}"]`)))
const openedOrder = (await keys())
await shot('opened-four-workers-under-the-lead')

// ── ran: the first turn lands and the row does not move ──────────────────────
await load('&minted=1&ran=1')
await page.waitForSelector(`[data-testid="conductor-child-count-${LEAD}"]`)
await page.click(`[data-testid="conductor-chevron-${LEAD}"]`)
await page.waitForSelector(`[data-slot-key="${WORKERS[0]}"]`)
await page.waitForTimeout(400)
console.log('ran:   ', await keys())
for (const [i, w] of WORKERS.entries()) {
  const r = await rowOf(w)
  check(`ran: ${w} is still one level under the lead`, r?.depth === '1', `depth=${r?.depth}`)
  check(`ran: ${w} did not change depth when the authority changed hands`,
    r?.depth === openedDepths[i], `was=${openedDepths[i]} now=${r?.depth}`)
}
check('ran: the row ORDER is unchanged, so nothing visibly moved',
  (await keys()) === openedOrder, `was="${openedOrder}" now="${await keys()}"`)
check('ran: no orphan glyph', (await orphanGlyphs()) === 0)
// The unrelated chat was nobody's child and stays a root in every frame.
const control = await rowOf(UNRELATED)
check('control: the unrelated chat stays a top-level row', control?.depth === '0',
  `depth=${control?.depth}`)
await shot('ran-first-turn-same-nesting')

await finish()
