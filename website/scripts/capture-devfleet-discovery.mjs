/**
 * Screenshots of Dev Fleet's two checkout-discovery states.
 *
 * Drives the ISOLATED capture entry (website/capture/devfleet-discovery.html),
 * which mounts the REAL DevFleetPage with `fetch` stubbed at the network seam to
 * serve the `/fleet` payloads the backend sends for each state. Every scene
 * asserts its headline copy before shooting, so this can never quietly emit a
 * screenshot of an error boundary or the wrong branch.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-devfleet-discovery.mjs http://127.0.0.1:6812 ../temp-screenshots/devfleet-repo-discovery
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/devfleet-repo-discovery'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

async function shoot(scene, theme, mustSee, name) {
  await page.goto(`${BASE}/capture/devfleet-discovery.html?scene=${scene}&theme=${theme}`)
  for (const text of mustSee) {
    await page.waitForSelector(`text=${text}`, { timeout: 15000 })
  }
  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: false })
  console.log(`captured ${name}`)
}

// No checkout found anywhere: a question the user can answer, not a red failure
// against a path they never chose.
await shoot('setup', 'dark', ['No Kiro Crew checkout found', 'KIROCREW_DEVFLEET_REPO='],
  '01-needs-setup-dark.png')
await shoot('setup', 'light', ['No Kiro Crew checkout found'], '02-needs-setup-light.png')

// A checkout WAS named and git cannot read it: still an error, and it still
// names the path, because that path came from the user's own configuration.
// The path is asserted without its leading slash: `text=/…` is Playwright's
// regex form and would match nothing here.
await shoot('error', 'dark', ['Discovery Error', 'checkouts/kirocrew'],
  '03-configured-path-unreadable-dark.png')

await browser.close()
