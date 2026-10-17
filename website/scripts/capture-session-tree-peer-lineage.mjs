/**
 * Screenshot harness, and behaviour check, for the conductor lane over FEDERATED rows:
 * sessions a connected crew lists through the hub's chat-slots route.
 *
 * Three frames over the same peer listing through the REAL `ChatSidebar` and the REAL
 * `useInstanceSessions`:
 *   before -- the chat-slots reply carries no `parent`, which is what `_clean_peer_slot`
 *             sent before this change: no row cites anyone, so the conductor lane is
 *             not even offered and the list is flat -- a run of `On worker-1` rows
 *             with no chevron and nothing to fold.
 *   after  -- the reply carries `parent: {slot, key}` as the hub now forwards it: the
 *             peer conductor is ONE row with its worker count and the subtree's badge,
 *             SHUT by default.
 *   opened -- one press on that row's chevron, and every worker nests under it.
 *
 * The after frame is a fresh page load with `?parent=1`, not a payload swap on the
 * mounted page: in the product the hub either forwards the citation or it does not,
 * so the two payloads never meet in one session. Swapping them in place would read
 * every null -> key transition as a re-parent (`citedCreatorRef`) and auto-open the
 * crew, photographing an adopt rather than the collapsed default.
 *
 * Serves the capture page from the DEV server (`/capture/session-tree-peer-lineage.html`),
 * with every other `/api/**` boot fixture answered by the shared stub.
 *
 * Usage: node scripts/capture-session-tree-peer-lineage.mjs [devBase] [outDir]
 */
import { openSessionTreeHarness } from './lib/session-tree-harness.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6181'
const OUT = process.argv[3] || '../temp-screenshots/session-tree-peer-lineage'
const PEER = 'worker-1'
const LEAD = 'chat-2201'
const LEAD_ID = `${PEER}:${LEAD}`
const WORKERS = ['chat-2202', 'chat-2203', 'chat-2204']

const { page, check, rows, keys, rowOf, settleTheme, shot, finish } = await openSessionTreeHarness(OUT)
const orphanGlyphs = () => page.$$eval('[data-testid^="conductor-orphan-"]', els => els.length)

async function load(query) {
  await page.goto(`${BASE}/capture/session-tree-peer-lineage.html?theme=dark${query}`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector(`[data-slot-key="${LEAD}"]`)
  await settleTheme()
  await page.waitForTimeout(400)
}

// ── before: the reply carries no citation ─────────────────────────────────────
await load('')
console.log('before:', await keys())
check('before: every row is in the list', (await rows()).length === 6, `rows=${(await rows()).length}`)
check('before: the conductor lane is not offered -- nothing cites anyone, so no tree',
  !(await page.$('[data-testid="conductor-view-lane"]')))
check('before: the peer conductor has no chevron and counts nothing',
  !(await page.$(`[data-testid="conductor-child-count-${LEAD_ID}"]`)))
await shot('before-peer-workers-stray')

// ── after: the reply carries parent as the hub now forwards it ─────────────────
await load('&parent=1')
await page.waitForSelector(`[data-testid="conductor-child-count-${LEAD_ID}"]`)
console.log('after: ', await keys())
const lead = await rowOf(LEAD)
check('after: the peer conductor is a top-level row', lead?.depth === '0', `depth=${lead?.depth}`)
for (const w of WORKERS) check(`after: ${w} is behind the chevron by default`, !(await rowOf(w)))
const count = await page.$eval(`[data-testid="conductor-child-count-${LEAD_ID}"]`, el => el.textContent)
check('after: the shut conductor counts its workers', count === String(WORKERS.length), `count=${count}`)
const needsYou = await page.$eval(`[data-testid="conductor-needs-you-${LEAD_ID}"]`, el => el.textContent).catch(() => null)
check('after: the shut conductor carries the worker awaiting approval', needsYou === '1', `needsYou=${needsYou}`)
const running = await page.$eval(`[data-testid="conductor-running-${LEAD_ID}"]`, el => el.textContent).catch(() => null)
check('after: the shut conductor carries the running worker', running === '1', `running=${running}`)
check('after: no orphan glyph', (await orphanGlyphs()) === 0)
await shot('after-peer-conductor-collapsed-by-default')

// ── opened: one press shows the workers under the peer conductor ──────────────
await page.click(`[data-testid="conductor-chevron-${LEAD_ID}"]`)
await page.waitForSelector(`[data-slot-key="${WORKERS[0]}"]`)
await page.waitForTimeout(400)
console.log('opened:', await keys())
for (const w of WORKERS) {
  const r = await rowOf(w)
  check(`opened: ${w} nests one level under the peer conductor`, r?.depth === '1', `depth=${r?.depth}`)
}
check('opened: the count stays with the row', !!(await page.$(`[data-testid="conductor-child-count-${LEAD_ID}"]`)))
check('opened: the aggregate leaves with the fold', !(await page.$(`[data-testid="conductor-needs-you-${LEAD_ID}"]`)))
// The hub's own local rows are untouched in every frame.
for (const k of ['chat-2190', 'chat-2188']) {
  const r = await rowOf(k)
  check(`control: local ${k} stays a top-level row`, r?.depth === '0', `depth=${r?.depth}`)
}
await shot('after-peer-conductor-opened-nests-workers')

await finish()
