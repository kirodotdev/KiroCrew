/**
 * Screenshots of Dev Fleet provision reattach-after-reload (issue #321).
 *
 * Drives the ISOLATED capture entry (website/capture/devfleet-provision-reattach.html),
 * which mounts the REAL DevFleetPage with `fetch` stubbed at the network seam to
 * serve the `/fleet` payload (carrying `provision_run_id`) and the `/run` polls.
 * Each page load in this script IS the scenario under review — a fresh mount
 * reattaching to a server-side provision run — so the evidence exercises the
 * exact useEffect + poll-loop code path the PR adds.
 *
 * Both scenes assert their headline state before shooting, so this can never
 * quietly emit a screenshot of an error boundary or an idle row.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-devfleet-reattach.mjs http://127.0.0.1:6811 ../temp-screenshots/devfleet-provision-reattach-321
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/devfleet-provision-reattach-321'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

async function shoot(scene, mustSee, name) {
  await page.goto(`${BASE}/capture/devfleet-provision-reattach.html?scene=${scene}&theme=dark`)
  for (const text of mustSee) {
    await page.waitForSelector(`text=${text}`, { timeout: 15000 })
  }
  await page.waitForTimeout(400) // let the elapsed counter + log settle
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: false })
  console.log(`captured ${name}`)
}

// Reload mid-provision: the stepper and live log are rehydrated from the
// fleet payload's provision_run_id — the state that used to be lost.
await shoot('running', ['Provisioning', 'kc-wt-oauth-device-flow'], '01-reattach-running-stepper-dark.png')

// Reload after a failed provision: the red failure strip and auto-expanded
// log persist, restoring the failure evidence a reload used to destroy.
await shoot('failed', ['Provision failed (exit 1)', 'npm ERR! code 1'], '02-reattach-failed-persisted-dark.png')

await browser.close()
