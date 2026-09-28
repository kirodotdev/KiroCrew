/** Real-browser evidence for the EXPANDED view's FIRST-MINT failure.
 *
 * Drives website/capture/crew-webview-expanded-mint-fail-first.html, which
 * mounts the REAL `CrewWebview` over a gateway whose every mint refuses. The
 * script expands once and the refusal is the first thing that happens: no
 * document exists, so the band must carry the hard "could not be rendered"
 * sentence (not the refresh one), the frame slot must hold nothing (no
 * "Rendering the dashboard" line under a failure), and the retry must be there.
 * UX review asked for this state by name -- shot-04 is the docked read error and
 * shot-07 the retained-document case, and neither shows the band over an empty
 * frame.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-expanded-mint-fail-first.mjs http://127.0.0.1:6824 ../temp-screenshots/crew-webview
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
await page.goto(`${BASE}/capture/crew-webview-expanded-mint-fail-first.html?theme=dark`, {
  waitUntil: 'domcontentloaded',
})

const expand = page.locator('[data-testid="crew-webview-expand"]')
await expand.waitFor({ state: 'visible', timeout: 15000 })
await expand.click()

const bar = page.locator('[data-testid="crew-webview-mint-error"]')
await bar.waitFor({ state: 'visible', timeout: 15000 })
check('the refused first mint raises the failure band', await bar.isVisible())

// The hard sentence, not the refresh one: nothing has rendered, so "showing the
// last version" would be a claim about a document that does not exist.
const barText = (await bar.textContent()) || ''
check('the band says the dashboard could not be rendered', barText.includes('could not be rendered'))
check('the band does not claim an older version is showing', !barText.includes('Showing the'))

// Nothing behind the band: no frame was ever minted, and the in-flight line is
// withheld once a mint has failed and no retry is running -- the two lines used
// to contradict each other here.
const expanded = page.locator('[data-expanded="true"]')
check('no document frame exists behind the band', (await expanded.locator('iframe').count()) === 0)
check(
  'the "Rendering the dashboard" line is withheld under the failure',
  !((await expanded.textContent()) || '').includes('Rendering the dashboard'),
)
check(
  'the band offers a way back',
  await expanded.locator('button', { hasText: 'Try again' }).isVisible(),
)
check('the collapse control stays enabled', await page.locator('[data-testid="crew-webview-collapse"]').isEnabled())

await page.waitForTimeout(200)
await page.screenshot({ path: `${OUT}/08-expanded-mint-fail-first.png` })

await page.close()
await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/08-expanded-mint-fail-first.png`)
