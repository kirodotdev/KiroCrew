/**
 * Screenshot of the Dev Fleet provision failure notice naming the failing
 * step from its stderr tail.
 *
 * Drives the `steperr` scene of the ISOLATED reattach capture entry
 * (website/capture/devfleet-provision-reattach.html), which mounts the REAL
 * DevFleetPage with `fetch` stubbed at the network seam to serve a `/fleet`
 * payload carrying a failed `provision_run_id` and the `/run` record of a
 * refused `pip install`: diagnostic on stderr, block-buffered stdout progress
 * line flushed after it, then the runner's `::steperr::` markers.
 *
 * The scene asserts the notice's headline before shooting, and asserts what the
 * notice must NOT say (the stdout progress line, the CLI's closing line, a raw
 * marker), so it can never quietly emit a screenshot of the wrong state.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-devfleet-provision-steperr.mjs http://127.0.0.1:6813 ../temp-screenshots/devfleet-provision-steperr [suffix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/devfleet-provision-steperr'
const SUFFIX = process.argv[4] || 'after'
const ASSERT_FIXED = SUFFIX !== 'before'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

await page.goto(`${BASE}/capture/devfleet-provision-reattach.html?scene=steperr&theme=dark`)
await page.waitForSelector('text=Provision failed (exit 1)', { timeout: 15000 })
await page.waitForTimeout(400)

const notice = page.getByTestId('provision-error-kc-wt-oauth-device-flow')
const text = await notice.textContent()
if (ASSERT_FIXED) {
  for (const must of ['Permission denied', 'Consider using the `--user` option']) {
    if (!text.includes(must)) throw new Error(`notice does not name the diagnostic: ${text}`)
  }
  for (const mustNot of ['see output above', 'Installing build dependencies', '::steperr::']) {
    if (text.includes(mustNot)) throw new Error(`notice still names the wrong line: ${text}`)
  }
}
await page.screenshot({ path: `${OUT}/provision-failure-notice-${SUFFIX}.png`, fullPage: false })
console.log(`captured provision-failure-notice-${SUFFIX}.png :: ${text}`)

await browser.close()
