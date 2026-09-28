/**
 * Isolated capture for the crew webview's HEALTHY EXPANDED state -- shot 02.
 *
 * WHY THIS EXISTS: 02 was the one shot in this set with no script behind it, so
 * it could not be regenerated when anything it shows changed. It embeds the
 * stand-in document, so a fixture edit silently staled it, and UX review has
 * twice blocked this PR on screenshots that showed superseded states. A shot
 * nobody can reproduce is evidence with a shelf life.
 *
 * It reuses the transition entry, whose gateway mints successfully, so this walks
 * the same sequence a reader does: expand, the mint lands, the document paints.
 * No failure is injected -- that is 07's job.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-expanded.mjs http://127.0.0.1:6831 ../temp-screenshots/crew-webview
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/crew-webview'
mkdirSync(OUT, { recursive: true })

// The expanded view is `fixed inset-0`, so the viewport IS the shot.
const VIEWPORT = { width: 1120, height: 720 }

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

const expand = page.locator('[data-testid="crew-webview-expand"]')
await expand.waitFor({ state: 'visible', timeout: 15000 })
await expand.click()

const frame = page.locator('[data-expanded="true"] iframe')
await frame.waitFor({ state: 'visible', timeout: 15000 })
check('the mint landed and the frame is up', await frame.isVisible())

// Read INSIDE the frame: a visible iframe element proves nothing about whether a
// document painted, and an empty frame would still photograph plausibly.
const inner = page.frameLocator('[data-expanded="true"] iframe').locator('body')
await inner.waitFor({ state: 'visible', timeout: 15000 })
const innerText = (await inner.textContent()) || ''
check('the document actually painted', innerText.includes('Sources read'))

// No failure band in the healthy state: this shot exists to show the state
// WITHOUT one, and a band creeping in would make it a duplicate of 07.
check(
  'no failure band in the healthy state',
  (await page.locator('[data-testid="crew-webview-mint-error-band"]').count()) === 0,
)

// The chrome's own freshness label and the document's must agree. They are
// different sources -- the bar reads the record's `published_at`, the document
// prints whatever the crew wrote -- and a reader who sees two different ages
// cannot tell how old the page is. That disagreement is what UX review measured
// on 07, and the fixture used to carry it here too.
const bar = (await page.locator('[data-testid="crew-webview-age"]').textContent()) || ''
check('the bar dates the record', bar.includes('Published'))
check(
  'the document does not contradict the bar',
  !/\d+\s*(second|minute|hour)s?\s*ago/i.test(innerText),
)

await page.waitForTimeout(250)
await page.screenshot({ path: `${OUT}/02-expanded-dashboard.png` })

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/02-expanded-dashboard.png`)
