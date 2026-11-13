/**
 * Screenshots of the shared change card layout from the isolated capture entry
 * website/capture/change-card-layout.html: a goal add, a setting edit and a goal whose
 * Affects row names more than the title, each
 * wide (720px) and narrow (360px), light and dark.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort   # in another shell
 *   node scripts/capture-change-card-layout.mjs http://127.0.0.1:6816 ../temp-screenshots/change-card-layout
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6816'
const OUT = process.argv[3] || '../temp-screenshots/change-card-layout'
mkdirSync(OUT, { recursive: true })

const SCENES = ['goal', 'edit', 'shared']
const THEMES = ['dark', 'light']
const WIDTHS = [720, 360]

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
for (const scene of SCENES) {
  for (const width of WIDTHS) {
    for (const theme of THEMES) {
      const page = await browser.newPage({ viewport: { width: width + 40, height: 700 }, deviceScaleFactor: 2 })
      page.on('pageerror', e => { console.error(`  pageerror: ${e.message}`); failures++ })
      await page.goto(`${BASE}/capture/change-card-layout.html?scene=${scene}&theme=${theme}&width=${width}`, { waitUntil: 'load', timeout: 180_000 })
      await page.locator('[data-testid="change-card"]').waitFor({ timeout: 180_000 })
      await page.locator('[data-testid="change-card-footer"]').waitFor()
      const root = page.locator('[data-capture-root]')
      await root.screenshot({ path: `${OUT}/${scene}-${width}-${theme}.png` })
      console.log(scene, width, theme, JSON.stringify((await root.textContent())?.slice(0, 240)))
      await page.close()
    }
  }
}
await browser.close()
console.log(`wrote ${OUT}`)
process.exit(failures ? 1 : 0)
