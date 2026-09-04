/**
 * Screenshots for batch settings-2 of the ErrorNotice sweep, via
 * capture/error-notice-settings-2.html (a Vite dev server must be serving the
 * website root).
 *
 *   node scripts/capture-error-notice-settings-2.mjs <viteBase> <outDir> [before|after]
 *
 * `after` (default) asserts what the migrated panels render: the failure text
 * lives in `role="alert"` elements (ErrorNotice); the Webex header and save
 * failures — beside the unsaved token draft — carry NO hand-off, while the
 * Secrets read failure (no draft open) carries one. `before` asserts the
 * pre-migration shape (no `role="alert"`, no hand-off) so the two frame sets
 * cannot be mixed up. A scene whose state does not match writes no frame: a
 * misleading screenshot is worse than none.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-settings-2'
const PHASE = process.argv[4] === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { scene: 'webex', theme: 'dark', save: true },
  { scene: 'webex', theme: 'light', save: true },
  { scene: 'secrets', theme: 'dark', save: false },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 960, height: 720 }, deviceScaleFactor: 2 })
let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/error-notice-settings-2.html?scene=${s.scene}&theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  if (s.scene === 'webex') {
    await page.getByText(/connection failed/i).first().waitFor({ timeout: 15_000 })
    if (s.save) {
      await page.getByRole('button', { name: /^save/i }).first().click()
      await page.getByText(/401 Unauthorized/).first().waitFor({ timeout: 15_000 })
    }
  } else {
    // Before: the list read failure rendered as "No secrets stored yet." After:
    // the read failure is named. Wait for the query to settle either way.
    await page.getByText(/no secrets stored yet|could not load secrets/i).first().waitFor({ timeout: 15_000 })
  }
  const alerts = await page.locator('[role="alert"]').count()
  const handoffs = await page.getByRole('button', { name: /^ask the agent$/i }).count()
  let ok
  if (PHASE === 'after') {
    ok = s.scene === 'webex'
      ? alerts === 2 && handoffs === 0 // header connect_error + inline save error, both No hand-off
      : alerts === 1 && handoffs === 1 // list read failure: no add form open, hand-off on
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
