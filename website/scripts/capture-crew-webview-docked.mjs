/**
 * Isolated capture for the crew webview's DOCKED summary -- shot 01.
 *
 * WHY THIS EXISTS: 01 was the other shot in this set with no script behind it (02
 * was the first; see capture-crew-webview-expanded.mjs), so it could not be
 * regenerated when the docked card changed. UX review has blocked this PR twice
 * on screenshots showing superseded states, and the docked card carries the
 * freshness chip that review asked to relabel -- exactly the kind of change a
 * hand-made shot hides.
 *
 * Uses the transition entry's gateway, which serves a published record, and
 * photographs the card WITHOUT expanding: no document is minted here, which is
 * itself part of the contract (the drawer is free until someone opens it).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-docked.mjs http://127.0.0.1:6831 ../temp-screenshots/crew-webview
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/crew-webview'
mkdirSync(OUT, { recursive: true })

// A right-dock-sized frame: the width the drawer occupies beside the members list.
const VIEWPORT = { width: 360, height: 480 }

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 })
page.on('pageerror', e => {
  console.error('pageerror:', e.message)
  failures++
})
await page.goto(`${BASE}/capture/crew-webview-transition.html?theme=dark`, {
  waitUntil: 'domcontentloaded',
})

const card = page.locator('[data-testid="crew-webview-summary"]')
await card.waitFor({ state: 'visible', timeout: 15000 })
check('the docked card is up', await card.isVisible())

// No document while docked. This is the contract that makes the drawer free, and
// a frame appearing here would mean minting on render rather than on expand.
check('no document is minted while docked', (await page.locator('iframe').count()) === 0)

// The chip says WHAT it dates. Bare, it sat beside "Contained" and read as the age
// of that status rather than of the dashboard, which a reader told UX review.
const chip = (await page.locator('[data-testid="crew-webview-age"]').textContent()) || ''
check('the chip names what it dates', chip.includes('Published'))

// And the expand control is offered, since the card's whole job is to lead there.
check(
  'the card offers the expand control',
  (await page.locator('[data-testid="crew-webview-expand"]').count()) === 1,
)

await page.waitForTimeout(200)
await page.screenshot({ path: `${OUT}/01-docked-summary.png` })

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/01-docked-summary.png`)
