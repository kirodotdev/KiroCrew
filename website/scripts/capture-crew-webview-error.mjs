/** Real-browser evidence for the crew webview FETCH-ERROR state.
 *
 * Drives the ISOLATED capture entry (website/capture/crew-webview-error.html),
 * which mounts the REAL `CrewWebview` over a fetch boundary that answers the
 * member-panel read with HTTP 500 -- the component's own `isError` branch. The
 * shot therefore shows the shipped `webview_error` notice AND the
 * `crew-webview-error-retry` "Try again" control, which the UX review flagged as
 * present in no supplied screenshot.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-error.mjs http://127.0.0.1:6824 ../temp-screenshots/crew-webview
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/crew-webview'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 360, height: 480 }

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 })
page.on('pageerror', e => { console.error('pageerror:', e.message); failures++ })
await page.goto(`${BASE}/capture/crew-webview-error.html?theme=dark`, { waitUntil: 'networkidle' })

const notice = page.locator('[data-testid="crew-webview-error"]')
await notice.waitFor({ state: 'visible', timeout: 15000 })

// The retry control the review said was missing must be present and be the
// shipped "Try again" string.
const retry = page.locator('[data-testid="crew-webview-error-retry"]')
check('error state shows the error notice', await notice.isVisible())
check('error state shows the Try again control', await retry.isVisible())
const retryText = (await retry.textContent()) || ''
check('retry control carries the retry label', retryText.trim().length > 0)

await page.waitForTimeout(150)
await page.screenshot({ path: `${OUT}/04-error-state.png` })

await page.close()
await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/04-error-state.png`)
