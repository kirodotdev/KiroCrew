/**
 * Evidence shots for the muted-channel disclosure label.
 *
 * Asserts the RENDERED label before every shot, so a stale bundle or a wrong
 * catalog lookup fails the run instead of silently photographing the old copy.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6853 --strictPort   # in another shell
 *   node scripts/capture-muted-disclosure.mjs [base] [outDir] [expectCollapsed]
 *
 * `expectCollapsed` names the label the collapsed button MUST show: pass
 * "Muted (1)" when capturing the pre-fix tree and "Show muted (1)" after.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6853'
const OUT = process.argv[3] || '../temp-screenshots/muted-count-strings'
const EXPECT_COLLAPSED = process.argv[4] || 'Show muted (1)'
const AFTER = EXPECT_COLLAPSED !== 'Muted (1)'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 460, height: 380 }, deviceScaleFactor: 2 })
await page.goto(`${BASE}/capture/muted-disclosure.html?theme=dark`, { waitUntil: 'networkidle' })
await page.waitForSelector('[data-capture-root]')

const chip = page.locator('button[aria-pressed]').first()
await chip.waitFor({ state: 'visible', timeout: 15_000 })

/** Shoot only after the label and the pressed state both read as expected. */
async function shot(name, expectLabel, expectPressed) {
  const label = (await chip.textContent()).trim()
  const pressed = await chip.getAttribute('aria-pressed')
  if (label !== expectLabel) throw new Error(`label is ${JSON.stringify(label)}, expected ${JSON.stringify(expectLabel)}`)
  if (pressed !== expectPressed) throw new Error(`aria-pressed is ${pressed}, expected ${expectPressed}`)
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}.png` })
  console.log(`wrote ${OUT}/${name}.png  label=${JSON.stringify(label)} aria-pressed=${pressed}`)
}

await shot(AFTER ? 'after-collapsed' : 'before-collapsed', EXPECT_COLLAPSED, 'false')
await chip.click()
await page.waitForSelector('button[aria-pressed="true"]', { timeout: 10_000 })
await page.waitForTimeout(250)
// Pre-fix the label does not move, which is the whole defect -- so the expanded
// frame expects the SAME string there and the new one after.
await shot(AFTER ? 'after-expanded' : 'before-expanded', AFTER ? 'Hide muted (1)' : 'Muted (1)', 'true')

await browser.close()
