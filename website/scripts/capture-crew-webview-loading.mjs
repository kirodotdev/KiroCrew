/** Real-browser evidence for the crew webview LOADING (skeleton) state.
 *
 * Drives the ISOLATED capture entry (website/capture/crew-webview-loading.html),
 * which mounts the REAL `CrewWebview` over a fetch boundary whose panel read
 * never resolves -- the component's own `isLoading` skeleton branch. The shot
 * shows the pulsing placeholder the UX review said appeared in no screenshot.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-loading.mjs http://127.0.0.1:6824 ../temp-screenshots/crew-webview
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
// The read never resolves, so 'networkidle' would hang; wait for DOM instead.
await page.goto(`${BASE}/capture/crew-webview-loading.html?theme=dark`, { waitUntil: 'domcontentloaded' })

const skeleton = page.locator('[data-testid="crew-webview-loading"]')
await skeleton.waitFor({ state: 'visible', timeout: 15000 })
check('loading state shows the skeleton', await skeleton.isVisible())

await page.waitForTimeout(200)
await page.screenshot({ path: `${OUT}/05-loading-state.png` })

await page.close()
await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/05-loading-state.png`)
