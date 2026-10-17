/**
 * Screenshots for batch settings-1 of the ErrorNotice sweep, via
 * capture/error-notice-settings-1.html (a Vite dev server must be serving the
 * website root).
 *
 *   node scripts/capture-error-notice-settings-1.mjs <viteBase> <outDir> [before|after]
 *
 * `after` (default) asserts what the migrated panels render: the failure text
 * lives in `role="alert"` elements (ErrorNotice), the load failure carries the
 * "Ask the agent" hand-off, and the save failure — beside the unsaved token
 * draft — does NOT. `before` asserts the pre-migration shape (no `role="alert"`
 * at all) so the two frame sets cannot be mixed up. A scene whose state does not
 * match writes no frame: a misleading screenshot is worse than none.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-settings-1'
const PHASE = process.argv[4] === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { scene: 'telegram', theme: 'dark', save: true },
  { scene: 'telegram', theme: 'light', save: true },
  { scene: 'imessage', theme: 'dark', save: false },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 960, height: 720 }, deviceScaleFactor: 2 })
let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/error-notice-settings-1.html?scene=${s.scene}&theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  if (s.scene === 'telegram') {
    await page.getByText(/failed to start/i).first().waitFor({ timeout: 15_000 })
    if (s.save) {
      await page.getByRole('button', { name: /^save/i }).first().click()
      await page.getByText(/401 Unauthorized/).first().waitFor({ timeout: 15_000 })
    }
  } else {
    await page.getByText(/cannot load/i).first().waitFor({ timeout: 15_000 })
  }
  const alerts = await page.locator('[role="alert"]').count()
  const handoffs = await page.getByRole('button', { name: /^ask the agent$/i }).count()
  let ok
  if (PHASE === 'after') {
    ok = s.scene === 'telegram'
      ? alerts === 2 && handoffs === 0 // header connect_error + inline save error, both No hand-off
      : alerts === 1 && handoffs === 1 // load failure: whole form withheld, hand-off on
  } else {
    ok = alerts === 0 && handoffs === 0
  }
  console.log(`${PHASE}/${s.scene}-${s.theme}: alerts=${alerts} handoffs=${handoffs} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${PHASE}-${s.scene}-${s.theme}.png` })
}
await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected state -- no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} ${PHASE} screenshots to ${OUT}`)
