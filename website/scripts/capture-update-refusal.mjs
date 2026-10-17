/**
 * Screenshots of the three update-failure surfaces (PR evidence for the
 * interpreter-floor refusal):
 *
 *   modal-voluntary-refused  — "Update now" on a voluntary update answered 409
 *                              `python_floor`; the refusal renders in the modal.
 *   modal-required-refused   — the same refusal on a MANDATORY update: no
 *                              dismissal, no hand-off, installer command stays.
 *   overlay-error            — the apply worker pushed the `error` step after
 *                              accepting the request; the overlay must end.
 *
 * Drives the ISOLATED capture entries (website/capture/update-found-popup.html,
 * website/capture/update-overlay.html); see those files for why the full SPA is
 * not used. Each scene asserts a marker and the script EXITS NONZERO when one
 * is missing, so it can never quietly emit a screenshot of the wrong state.
 *
 * Run once from the PR checkout and once from the base checkout (same scenes,
 * the base has none of the new affordances) to get the before/after pair:
 *
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort      # in another shell
 *   node scripts/capture-update-refusal.mjs http://127.0.0.1:6813 <outDir> [--baseline]
 *
 * `--baseline` relaxes the markers to what the base renders (the plain text
 * refusal, the stalled spinner) so the "before" frame can be captured from a
 * checkout that has none of the new test ids.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/update-python-floor'
const BASELINE = process.argv.includes('--baseline')
mkdirSync(OUT, { recursive: true })

const REFUSAL = 'Update refused'
const SCENES = [
  {
    file: BASELINE ? 'modal-voluntary-refused-before.png' : 'modal-voluntary-refused-after.png',
    url: '/capture/update-found-popup.html?scene=apply-refused&theme=dark&lang=en',
    click: 'button:has-text("Update now")',
    // After: the ErrorNotice slot with its "Ask the agent" hand-off.
    // Before: the same 409 as a bare red paragraph.
    marker: BASELINE ? `text=${REFUSAL}` : '[data-testid="update-found-apply-error"]',
    alsoVisible: BASELINE ? [] : ['text=Ask the agent'],
    absent: [],
  },
  {
    file: BASELINE ? 'modal-required-refused-before.png' : 'modal-required-refused-after.png',
    url: '/capture/update-found-popup.html?scene=required-refused&theme=dark&lang=en',
    click: 'button:has-text("Update now")',
    marker: BASELINE ? `text=${REFUSAL}` : '[data-testid="update-found-apply-error"]',
    // A mandatory update keeps its enforcement: the refusal is shown, the
    // hand-off is not offered, and the installer command remains.
    alsoVisible: ['text=cli.sh'],
    absent: BASELINE ? [] : ['text=Ask the agent'],
  },
  {
    file: BASELINE ? 'overlay-error-before.png' : 'overlay-error-after.png',
    url: '/capture/update-overlay.html?scene=error&theme=dark',
    click: null,
    // After: the failure card. Before: the overlay does not know `error`, so
    // the step list is still "in progress" — assert the generic heading only.
    marker: BASELINE ? 'text=Updating' : '[data-testid="update-overlay-error"]',
    alsoVisible: BASELINE ? [] : ['button:has-text("Dismiss")'],
    absent: [],
  },
]

const b = await chromium.launch()
let failed = 0
for (const s of SCENES) {
  const ctx = await b.newContext({ viewport: { width: 760, height: 640 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await page.goto(`${BASE}${s.url}`, { waitUntil: 'networkidle' })
  try {
    if (s.click) {
      await page.locator(s.click).first().click({ timeout: 10_000 })
    }
    await page.locator(s.marker).first().waitFor({ state: 'visible', timeout: 15_000 })
    for (const sel of s.alsoVisible) await page.locator(sel).first().waitFor({ state: 'visible', timeout: 5_000 })
    for (const sel of s.absent) {
      if (await page.locator(sel).count()) throw new Error(`${sel} must not render in this scene`)
    }
    // Let the failure state settle (spinner swap, notice mount) before the frame.
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${s.file}` })
    console.log(`captured ${OUT}/${s.file} (${s.marker} asserted)`)
  } catch (err) {
    failed += 1
    console.error(`FAILED ${s.file}: ${err instanceof Error ? err.message : String(err)}`)
  } finally {
    await ctx.close()
  }
}
await b.close()
if (failed) process.exit(1)
