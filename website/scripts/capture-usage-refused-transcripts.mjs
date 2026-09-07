/**
 * Screenshot of the usage page's refused-transcript warning (#6733), via the
 * capture/usage-refused-transcripts harness (which stubs only /api/usage/kiro
 * and renders the real UsageTab through the real acp adapter). Asserts the
 * warning text is present before shooting, so a regression that drops the
 * banner fails the capture instead of shipping a blank frame.
 *
 * Usage: node scripts/capture-usage-refused-transcripts.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'
import path from 'node:path'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/6733-unc-usage-refused'

const b = await chromium.launch()
for (const theme of ['dark', 'light']) {
  const p = await (await b.newContext({ viewport: { width: 760, height: 640 }, deviceScaleFactor: 2 })).newPage()
  await p.goto(`${base}/capture/usage-refused-transcripts.html?scene=refused&theme=${theme}`, { waitUntil: 'networkidle' })
  await p.getByText(/couldn't load/i).waitFor({ state: 'visible', timeout: 15_000 })
  const out = path.join(outDir, `usage-refused-warning-${theme}.png`)
  await p.screenshot({ path: out })
  console.log(`captured ${out} (warning text asserted)`)
  await p.close()
}
await b.close()
