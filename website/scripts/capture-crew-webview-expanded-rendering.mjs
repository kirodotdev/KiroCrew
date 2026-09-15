/** Real-browser evidence for the EXPANDED view's pre-mint rendering state.
 *
 * Drives website/capture/crew-webview-expanded-rendering.html, which mounts the
 * REAL `CrewWebview` over a gateway whose mint POST never settles. It clicks the
 * real expand control, so the shot is the state every first expand passes
 * through: the "Contained" bar with the `webview_rendering` line under it, which
 * the UX review said appeared in no screenshot.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-expanded-rendering.mjs http://127.0.0.1:6824 ../temp-screenshots/crew-webview
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
// The mint never settles, so 'networkidle' would hang; wait for DOM instead.
await page.goto(`${BASE}/capture/crew-webview-expanded-rendering.html?theme=dark`, {
  waitUntil: 'domcontentloaded',
})

const expand = page.locator('[data-testid="crew-webview-expand"]')
await expand.waitFor({ state: 'visible', timeout: 15000 })
await expand.click()

const dialog = page.locator('[data-expanded="true"]')
await dialog.waitFor({ state: 'visible', timeout: 15000 })
check('expand opens the dialog', await dialog.isVisible())

// The point of the shot: the rendering line, NOT an iframe.
const rendering = page.getByText('Rendering the dashboard', { exact: false })
await rendering.waitFor({ state: 'visible', timeout: 15000 })
check('pre-mint shows the rendering line', await rendering.isVisible())
check('pre-mint shows no iframe', (await page.locator('[data-expanded="true"] iframe').count()) === 0)
// A failure bar here would mean the mint settled as an error, which is the OTHER
// capture. Asserting its absence keeps the two shots from drifting into one.
check(
  'pre-mint shows no failure bar',
  (await page.locator('[data-testid="crew-webview-mint-error"]').count()) === 0,
)

await page.waitForTimeout(200)
await page.screenshot({ path: `${OUT}/06-expanded-rendering.png` })

await page.close()
await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/06-expanded-rendering.png`)
