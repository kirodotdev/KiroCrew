/**
 * Screenshot capture for issue #13850 — Settings search governance:
 * Decisions (Jev) judge controls hidden when `capabilities.decisions` is
 * not offered.
 *
 * Produces two PNG files:
 *   disabled.png — search query "which judge" with decisions_enabled false
 *                  (judge entries are absent from results)
 *   enabled.png  — same query with decisions_enabled true
 *                  (judge entries appear)
 *
 * Usage:
 *   node scripts/capture-13850-settings-search-governance.mjs [base] [out-dir]
 *   Default base: http://127.0.0.1:5199
 *   Start the dev server first:
 *     npx vite --host 127.0.0.1 --port 5199   (from the website/ directory)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../.github/screenshots/pr-13850'
mkdirSync(out, { recursive: true })

const scenes = [
  { name: 'disabled', label: 'decisions_enabled: false — judge entries absent from results' },
  { name: 'enabled',  label: 'decisions_enabled: true — judge entries appear' },
]

const browser = await chromium.launch()
try {
  for (const { name, label } of scenes) {
    const page = await browser.newPage({
      viewport: { width: 500, height: 400 },
      deviceScaleFactor: 2,
    })
    page.on('pageerror', err => { throw err })

    await page.goto(
      `${base}/capture/settings-search-governance-13850.html?scene=${name}&theme=dark`,
      { waitUntil: 'networkidle' },
    )

    // Wait for the governance query to settle and the search to render.
    await page.locator('[data-capture-root]').waitFor({ state: 'visible', timeout: 15_000 })

    // Type the query that exercises the governed entries.
    const input = page.locator('[data-capture-root] input[type="text"]')
    await input.waitFor({ state: 'visible', timeout: 10_000 })
    await input.fill('which judge')

    // Wait for the results list (present for both 'no results' and real hits).
    const listbox = page.locator('#settings-search-results')
    await listbox.waitFor({ state: 'visible', timeout: 8_000 })

    const file = `${out}/${name}.png`
    await page.screenshot({ path: file, animations: 'disabled' })
    console.log(`${name}: captured → ${file}  (${label})`)
    await page.close()
  }
} finally {
  await browser.close()
}
