/**
 * Acceptance probe for the fold-card scroll forwarding.
 *
 * The card sits in a `pointer-events-none` overlay that is a SIBLING of the
 * transcript scroller, so an interactive card is the wheel's target and the browser
 * finds no scrollable ancestor for it. Before the forwarder, a wheel over the card
 * left the scroller at scrollTop 0 while the same wheel over bare scroller moved it.
 *
 * This drives a REAL trusted wheel (page.mouse.wheel), over the grown card and over
 * bare scroller, and prints both deltas. Also checks the card is selectable, since
 * that is the thing being inert cost.
 */
import { chromium } from 'playwright'

const BASE = process.argv[2] || 'http://127.0.0.1:6820'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1000, height: 700 } })
await page.goto(`${BASE}/capture/pinned-prompt-handoff.html?theme=dark&tall=1`, { waitUntil: 'networkidle' })

const S = '[data-capture-scroller]'
const CARD = '[data-testid="pinned-prompt"]'

// Scroll until the tall prompt is mid-fold, i.e. the card is grown well past resting.
async function foldState() {
  return page.evaluate(({ s, c }) => {
    const sc = document.querySelector(s)
    const card = document.querySelector(c)
    const r = card?.getBoundingClientRect()
    return { scrollTop: sc?.scrollTop ?? -1, cardH: r ? Math.round(r.height) : 0, cardTop: r ? Math.round(r.top) : 0 }
  }, { s: S, c: CARD })
}

await page.evaluate(({ s }) => { document.querySelector(s).scrollTop = 540 }, { s: S })
await page.waitForTimeout(250)
const at = await foldState()
console.log('fold state:', JSON.stringify(at))
if (at.cardH < 200) { console.error('FAIL: card is not grown; probe would not test the covered case'); await browser.close(); process.exit(2) }

async function wheelOver(sel, dy) {
  const box = await page.locator(sel).first().boundingBox()
  if (!box) throw new Error(`no box for ${sel}`)
  // Aim at the middle of the card, which is the region that used to be dead.
  await page.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, 300))
  const before = await page.evaluate(({ s }) => document.querySelector(s).scrollTop, { s: S })
  await page.mouse.wheel(0, dy)
  await page.waitForTimeout(200)
  const after = await page.evaluate(({ s }) => document.querySelector(s).scrollTop, { s: S })
  return Math.round(after - before)
}

const overCard = await wheelOver(CARD, 400)
await page.evaluate(({ s }) => { document.querySelector(s).scrollTop = 540 }, { s: S })
await page.waitForTimeout(200)
// Bare scroller control: far left of the viewport, outside the card's max-w column.
const bare = await (async () => {
  await page.mouse.move(60, 400)
  const before = await page.evaluate(({ s }) => document.querySelector(s).scrollTop, { s: S })
  await page.mouse.wheel(0, 400)
  await page.waitForTimeout(200)
  const after = await page.evaluate(({ s }) => document.querySelector(s).scrollTop, { s: S })
  return Math.round(after - before)
})()

// Selection: the thing an inert card could not do.
const selectable = await page.evaluate(({ c }) => {
  const card = document.querySelector(c)
  const p = card?.querySelector('p')
  if (!p) return null
  const sel = window.getSelection()
  const range = document.createRange()
  range.selectNodeContents(p)
  sel.removeAllRanges(); sel.addRange(range)
  const text = sel.toString().trim()
  return { chars: text.length, head: text.slice(0, 40) }
}, { c: CARD })

console.log(`scrollTop delta, wheel OVER CARD : ${overCard}`)
console.log(`scrollTop delta, wheel OVER BARE : ${bare}`)
console.log('card text selectable:', JSON.stringify(selectable))

// The buttons UX flagged as dead-looking-but-live, checked against the chevron's
// CURRENT contract. That contract changed with the fold: while the fold holds the
// card it already renders the whole prompt, so the chevron is hidden rather than
// offering a click whose only visible effect would arrive once the fold ended. Both
// halves are asserted -- absent mid-fold, and live at rest. The second half is the
// dead-looking-but-live worry itself, so it must not be dropped.
await page.evaluate(({ s }) => { document.querySelector(s).scrollTop = 540 }, { s: S })
await page.waitForTimeout(200)
const midFoldChevrons = await page.locator(`${CARD} button[aria-expanded]`).count()

// Back to rest, where the card clamps again and the chevron is the way to the rest
// of the prompt. Measured in this harness: scrollTop 200 and 400 hold the card at its
// 48px resting height with the chevron present, 540 through 1200 are the fold (816px
// shrinking to 188px, chevron hidden), and 1521 is rest again. scrollTop 0 is BEFORE
// anything is pinned, so the card does not exist there at all.
await page.evaluate(({ s }) => { document.querySelector(s).scrollTop = 200 }, { s: S })
await page.waitForTimeout(250)
const restingChevrons = await page.locator(`${CARD} button[aria-expanded]`).count()
let beforeExpanded = null
let afterExpanded = null
if (restingChevrons > 0) {
  const chevron = page.locator(`${CARD} button[aria-expanded]`).first()
  beforeExpanded = await chevron.getAttribute('aria-expanded')
  await chevron.click({ timeout: 2000 }).catch(e => console.log('chevron click threw:', e.message))
  await page.waitForTimeout(250)
  afterExpanded = await chevron.getAttribute('aria-expanded')
}
const chevronHiddenMidFold = midFoldChevrons === 0
const chevronWorks = restingChevrons > 0 && beforeExpanded !== afterExpanded
console.log(`chevron mid-fold count ${midFoldChevrons} (hidden while folding: ${chevronHiddenMidFold})`)
console.log(`chevron at rest aria-expanded ${beforeExpanded} -> ${afterExpanded} (responds: ${chevronWorks})`)

await browser.close()
const ok = overCard > 0 && Math.abs(overCard - bare) <= Math.max(40, bare * 0.25)
  && (selectable?.chars ?? 0) > 100 && chevronHiddenMidFold && chevronWorks
console.log(ok ? 'RESULT: PASS — card forwards the wheel, and its text and buttons still work'
              : 'RESULT: FAIL — forwarding, selection or the buttons are not working')
process.exit(ok ? 0 : 1)
