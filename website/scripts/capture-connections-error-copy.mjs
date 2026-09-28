/**
 * Screenshot of the needs-attention banner copy fix: before (verdict copy over a
 * timeout, reconstructed from main), after-timeout (could-not-reach copy), and
 * after-invalid_grant (verdict copy preserved), in one frame. Waits for the
 * harness's data-capture-ready anchor before shooting.
 *
 * Usage: node scripts/capture-connections-error-copy.mjs <viteBase> <outFile>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../temp-screenshots/conn-honest-error/needs-attention-copy-before-after.png'

const b = await chromium.launch()
const p = await (await b.newContext({ viewport: { width: 560, height: 560 }, deviceScaleFactor: 2 })).newPage()
await p.goto(`${base}/capture/connections-error-copy.html?theme=dark`, { waitUntil: 'networkidle' })
await p.locator('[data-capture-ready]').waitFor({ state: 'visible', timeout: 15_000 })
await p.screenshot({ path: out, fullPage: true })
console.log(`captured ${out}`)
await b.close()
