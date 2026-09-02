/**
 * Screenshot of the desktop auto-download row after its migration from a bare
 * span+Toggle to SettingsToggle, via the existing capture/desktop-auto-download
 * harness (which stubs the Electron updater bridge). Asserts the row's
 * data-setting-label anchor — the SettingsToggle contract — before shooting.
 *
 * Usage: node scripts/capture-about-autodownload-row.mjs <viteBase> <outFile>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/settings-search-registry/about-auto-download-settingstoggle.png'

const b = await chromium.launch()
const p = await (await b.newContext({ viewport: { width: 900, height: 700 }, deviceScaleFactor: 2 })).newPage()
await p.goto(`${base}/capture/desktop-auto-download.html?scene=on&theme=dark`, { waitUntil: 'networkidle' })
const row = p.locator('[data-setting-label="Auto-update on restart"]')
await row.waitFor({ state: 'visible', timeout: 15_000 })
await p.screenshot({ path: out })
console.log(`captured ${out} (SettingsToggle anchor asserted)`)
await b.close()
