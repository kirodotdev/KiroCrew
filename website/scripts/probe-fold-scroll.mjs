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

// The buttons UX flagged as dead-looking-but-live. Clicking the chevron must actually
// toggle the card, not silently do nothing.
await page.evaluate(({ s }) => { document.querySelector(s).scrollTop = 540 }, { s: S })
await page.waitForTimeout(200)
const chevron = page.locator(`${CARD} button[aria-expanded]`).first()
const beforeExpanded = await chevron.getAttribute('aria-expanded')
await chevron.click({ timeout: 2000 }).catch(e => console.log('chevron click threw:', e.message))
await page.waitForTimeout(250)
const afterExpanded = await chevron.getAttribute('aria-expanded')
const chevronWorks = beforeExpanded !== afterExpanded
console.log(`chevron aria-expanded ${beforeExpanded} -> ${afterExpanded} (responds: ${chevronWorks})`)

await browser.close()
const ok = overCard > 0 && Math.abs(overCard - bare) <= Math.max(40, bare * 0.25)
  && (selectable?.chars ?? 0) > 100 && chevronWorks
console.log(ok ? 'RESULT: PASS — card forwards the wheel, and its text and buttons still work'
              : 'RESULT: FAIL — forwarding, selection or the buttons are not working')
process.exit(ok ? 0 : 1)
