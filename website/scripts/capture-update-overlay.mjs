/**
 * Screenshots of the update progress overlay's two mid-restart states (PR
 * evidence for the same-version-rebuild fix).
 *
 * Drives the ISOLATED capture entry (website/capture/update-overlay.html) —
 * see that file for why the full SPA is not used. Each scene asserts its own
 * marker and the script EXITS NONZERO if one is missing, so it can never
 * quietly emit a screenshot of the wrong state.
 *
 * Usage: node scripts/capture-update-overlay.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6807'
const OUT = process.argv[3] || '../temp-screenshots/update-restart-ux'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  {
    scene: 'restarting',
    // Socket still up: the idle waiting copy renders.
    marker: 'text=Page will reconnect when ready…',
    file: 'overlay-restarting-connected.png',
  },
  {
    scene: 'reconnecting',
    // Socket dropped mid-restart: the explicit reconnecting state this PR adds.
    marker: '[data-testid="update-reconnecting"]',
    file: 'overlay-restarting-disconnected.png',
  },
]

const b = await chromium.launch()
const page = await (await b.newContext({ viewport: { width: 760, height: 560 }, deviceScaleFactor: 2 })).newPage()
for (const { scene, marker, file } of SCENES) {
  await page.goto(`${BASE}/capture/update-overlay.html?scene=${scene}&theme=dark`, { waitUntil: 'networkidle' })
  const anchor = page.locator(marker)
  await anchor.waitFor({ state: 'visible', timeout: 15_000 })
  await page.screenshot({ path: `${OUT}/${file}` })
  console.log(`captured ${OUT}/${file} (${marker} asserted)`)
}
await b.close()
