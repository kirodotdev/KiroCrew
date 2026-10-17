/**
 * Screenshot harness, and behaviour check, for the conductor lane over a crew whose
 * CONDUCTOR WAS CLOSED while its three workers kept running.
 *
 * Three frames over the same crew through the REAL `ChatSidebar`:
 *   before -- the payload answers a null key for a creator that is not running, which
 *             is what the sidebar received before this change: three top-level rows
 *             wearing the orphan glyph while the lead that owns the run sits above
 *             them, open, and claims none of them.
 *   after  -- the payload walks up past the closed conductor to the lead and marks the
 *             edge `ancestor`. The lead is ONE row with its worker count, SHUT by
 *             default, which is the lane's standing rule for a crew.
 *   opened -- one press on the lead's chevron. All three nest one level under it and
 *             each still wears the closed-creator glyph, which now names a fact about
 *             the row rather than its position: the conductor opened it and is gone.
 *
 * Each frame is a fresh page load with its own query string rather than a payload swap
 * on the mounted page. The server answers one way or the other, never both in one
 * session, and a null -> key transition in place is read as a re-parent
 * (`citedCreatorRef`) which auto-opens the crew -- photographing an adopt instead of
 * the collapsed default.
 *
 * Serves the capture page from the DEV server
 * (`/capture/session-tree-closed-conductor.html`), with every `/api/**` boot fixture
 * answered by the shared stub.
 *
 * Usage: node scripts/capture-session-tree-closed-conductor.mjs [devBase] [outDir]
 */
import { openSessionTreeHarness } from './lib/session-tree-harness.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6181'
const OUT = process.argv[3] || '../temp-screenshots/session-tree-closed-conductor'
const LEAD = 'chat-2548'
const CONDUCTOR = 'chat-2552'
const CREW = ['chat-2554', 'chat-2555', 'chat-2556']
const SEPARATE = 'chat-2531'

const { page, check, rows, keys, rowOf, settleTheme, shot, finish } = await openSessionTreeHarness(OUT)
const citedBy = async key =>
  page.$eval(`[data-testid="conductor-orphan-${key}"]`, el => el.getAttribute('data-orphan-of'))
    .catch(() => null)

async function load(query) {
  await page.goto(`${BASE}/capture/session-tree-closed-conductor.html?theme=dark${query}`)
  await page.waitForSelector('[data-capture-ready]')
  await page.waitForSelector(`[data-slot-key="${LEAD}"]`)
  await settleTheme()
  await page.waitForTimeout(400)
}

// ── before: the closed creator leaves every worker at the top level ──────────
await load('')
console.log('before:', await keys())
check('before: every row is in the list', (await rows()).length === 5, `rows=${(await rows()).length}`)
for (const w of CREW) {
  const r = await rowOf(w)
  check(`before: ${w} is a top-level row`, r?.depth === '0', `depth=${r?.depth}`)
  check(`before: ${w} wears the orphan glyph for the closed conductor`,
    (await citedBy(w)) === CONDUCTOR, `cited=${await citedBy(w)}`)
}
check('before: the lead claims nobody',
  !(await page.$(`[data-testid="conductor-child-count-${LEAD}"]`)))
await shot('before-workers-orphaned-at-top-level')

// ── after: the walk reaches the lead, which is this change ───────────────────
await load('&reparent=1')
await page.waitForSelector(`[data-testid="conductor-child-count-${LEAD}"]`)
console.log('after: ', await keys())
const lead = await rowOf(LEAD)
check('after: the lead is a top-level row', lead?.depth === '0', `depth=${lead?.depth}`)
for (const w of CREW) check(`after: ${w} is behind the chevron by default`, !(await rowOf(w)))
const count = await page.$eval(`[data-testid="conductor-child-count-${LEAD}"]`, el => el.textContent)
check('after: the shut lead counts the whole crew', count === String(CREW.length), `count=${count}`)
await shot('after-crew-collapsed-under-the-lead')

// ── opened: nested, and still citing the conductor that is gone ──────────────
await page.click(`[data-testid="conductor-chevron-${LEAD}"]`)
await page.waitForSelector(`[data-slot-key="${CREW[0]}"]`)
await page.waitForTimeout(400)
console.log('opened:', await keys())
for (const w of CREW) {
  const r = await rowOf(w)
  check(`opened: ${w} nests one level under the lead`, r?.depth === '1', `depth=${r?.depth}`)
  check(`opened: ${w} still cites the closed conductor`, (await citedBy(w)) === CONDUCTOR,
    `cited=${await citedBy(w)}`)
  // The other glyph says the creator is OPEN and merely out of view, which would be
  // false here: a re-parented row must never claim its conductor is still running.
  check(`opened: ${w} does not claim its conductor is open`,
    !(await page.$(`[data-testid="conductor-cites-parent-${w}"]`)))
}
// The separate chat was nobody's child and is a root in every frame.
const control = await rowOf(SEPARATE)
check('control: the separate chat stays a top-level row', control?.depth === '0',
  `depth=${control?.depth}`)
await shot('opened-crew-nested-under-the-lead')

await finish()
