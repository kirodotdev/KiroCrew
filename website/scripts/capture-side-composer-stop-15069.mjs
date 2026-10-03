/**
 * Screenshot the Side Chat busy composer (issue #15069) for review evidence —
 * the reshaped control set Design and UX review asked to see rendered: the Stop
 * control on an empty busy composer, the steer/queue split button once a draft
 * is present, and the Refresh button reachable mid-turn.
 *
 * Drives the isolated capture entry (website/capture/side-composer-stop-15069.html),
 * which mounts the real ChatInput in its busy state. Writes a committed-evidence
 * PNG per theme under temp-screenshots/side-composer-stop-15069/ — the gitignored
 * root the UX blind-read / Screenshot Evidence lane reads from (force-add the
 * PNGs). The .github/ tree is off-limits on a fork PR (workflow-change guard).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6810 --strictPort   # in another shell
 *   node scripts/capture-side-composer-stop-15069.mjs http://127.0.0.1:6810 ../temp-screenshots/side-composer-stop-15069
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6810'
const OUT = process.argv[3] || '../temp-screenshots/side-composer-stop-15069'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failures = 0

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 460, height: 1300 }, deviceScaleFactor: 2 })
  await page.goto(`${BASE}/capture/side-composer-stop-15069.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-scene="empty-busy"]')
  await page.waitForSelector('[data-scene="draft-busy"]')
  await page.waitForSelector('[data-scene="settled-stop"]')
  await page.waitForSelector('[data-scene="stop-failed"]')

  // Assert the reshaped controls and settled states actually rendered, so a
  // blank/broken frame is a failure rather than silent empty evidence.
  const hasStop = await page.locator('[data-scene="empty-busy"] [data-testid="stop-button-armed"]').count()
  const hasSplit = await page.locator('[data-scene="draft-busy"] [data-testid="busy-send-caret"], [data-scene="draft-busy"] [data-testid="busy-send-button"]').count()
  const hasStopped = await page.locator('[data-scene="settled-stop"]').getByText('(side response stopped)').count()
  const hasFailNotice = await page.locator('[data-scene="stop-failed"]').getByText('Could not stop the side turn — try again').count()
  if (!hasStop) { console.error(`FAIL(${theme}): empty busy composer did not render the Stop control`); failures++ }
  if (!hasSplit) { console.error(`FAIL(${theme}): draft busy composer did not render the steer/queue split button`); failures++ }
  if (!hasStopped) { console.error(`FAIL(${theme}): settled transcript did not render the "(side response stopped)" row`); failures++ }
  if (!hasFailNotice) { console.error(`FAIL(${theme}): stop-failure notice did not render`); failures++ }

  await page.screenshot({ path: `${OUT}/${theme}.png`, fullPage: true })
  console.log(`wrote ${OUT}/${theme}.png (stop=${hasStop} split=${hasSplit} stopped=${hasStopped} fail=${hasFailNotice})`)
  await page.close()
}

await browser.close()
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
