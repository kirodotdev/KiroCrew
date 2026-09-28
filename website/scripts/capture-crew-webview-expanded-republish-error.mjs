/** Real-browser evidence for the EXPANDED view after a REPUBLISH whose re-mint fails.
 *
 * Drives website/capture/crew-webview-expanded-republish-error.html, which mounts
 * the REAL `CrewWebview` over a gateway that serves the fixture record first and a
 * record published three days LATER second, and whose mint lands once then
 * refuses. The script expands (first mint lands, document renders), triggers the
 * republish (the drawer's panel query is invalidated and refetches the newer
 * record; its mint fails; the older document stays up), and captures the one
 * arrangement earlier UX rounds called contradictory: the bar chip dates the NEW
 * record ("New version published ...") while the band dates the document on
 * screen ("Showing the version from ..."). Both ages are asserted to differ, so
 * the shot cannot pass by showing one age twice.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-expanded-republish-error.mjs http://127.0.0.1:6824 ../temp-screenshots/crew-webview
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
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
await page.goto(`${BASE}/capture/crew-webview-expanded-republish-error.html?theme=dark`, {
  waitUntil: 'domcontentloaded',
})

const expand = page.locator('[data-testid="crew-webview-expand"]')
await expand.waitFor({ state: 'visible', timeout: 15000 })
await expand.click()

const frame = page.locator('[data-expanded="true"] iframe')
await frame.waitFor({ state: 'visible', timeout: 15000 })
check('first mint renders the document', await frame.isVisible())
const inner = page.frameLocator('[data-expanded="true"] iframe').locator('body')
await inner.waitFor({ state: 'visible', timeout: 15000 })
check('the document actually painted', ((await inner.textContent()) || '').includes('Sources read'))

// The bar chip BEFORE the republish: one record, plain label. Read through the
// collapse button's row so the docked card's chip is not the one read.
const collapse = page.locator('[data-testid="crew-webview-collapse"]')
const barChip = collapse.locator('xpath=..').locator('[data-testid="crew-webview-age"]')
const before = (await barChip.textContent()) || ''
check('before the republish the chip carries the plain label', before.startsWith('Published '))

// The republish: a real refetch through react-query's invalidation. The stub
// answers with the newer record; its mint refuses; the old document stays.
await page.locator('[data-testid="capture-republish"]').dispatchEvent('click')

const bar = page.locator('[data-testid="crew-webview-mint-error"]')
await bar.waitFor({ state: 'visible', timeout: 15000 })
check('the refused re-mint raises the failure band', await bar.isVisible())
check('the older document is still behind the band', await frame.isVisible())
const bandText = (await bar.textContent()) || ''
check('the band dates the version on screen', bandText.includes('Showing the version from'))

// The chip now dates the NEWER record and says so.
await page.waitForFunction(
  () => {
    const c = document.querySelector('[data-testid="crew-webview-collapse"]')
    const chip = c && c.parentElement && c.parentElement.querySelector('[data-testid="crew-webview-age"]')
    return !!chip && (chip.textContent || '').startsWith('New version published')
  },
  null,
  { timeout: 15000 },
)
const after = (await barChip.textContent()) || ''
check('after the republish the chip says a new version was published', after.startsWith('New version published'))

// Two DIFFERENT ages: the chip's (new record) and the band's (shown document).
// Same-age would make the arrangement unreadable, which is what the fixture's
// three-day offset exists to prevent.
const chipAge = after.replace('New version published', '').trim()
const bandAge = (bandText.match(/Showing the version from (.+?)\./) || [])[1] || ''
check(`the two ages differ (chip "${chipAge}" vs band "${bandAge}")`, chipAge !== '' && bandAge !== '' && chipAge !== bandAge)
check(
  'the band offers a way back',
  await page.locator('[data-expanded="true"] button', { hasText: 'Try again' }).isVisible(),
)

await page.waitForTimeout(200)
await page.screenshot({ path: `${OUT}/09-expanded-republish-error.png` })

await page.close()
await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/09-expanded-republish-error.png`)
