/**
 * Screenshots of the three channel/version states in Settings > About, through
 * the capture/channel-move-pending harness (which stubs only the feed answer and
 * the Electron bridge -- see that entry for why neither is reachable otherwise).
 *
 * Each shot ASSERTS the anchor that proves the state before shooting, so a scene
 * that silently stopped rendering fails here instead of shipping a screenshot of
 * the wrong thing.
 *
 * Usage: node scripts/capture-channel-move.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/channel-move-pending'

const SCENES = [
  // The reported bug: the panel must NOT fold the chip, must not claim
  // up-to-date, and must name what stable publishes.
  { scene: 'gateway-move', anchor: '[data-testid="hero-channel-move-pending"]' },
  // The false positive: a promoted stable install has nothing pending.
  { scene: 'gateway-promoted', anchor: '[data-testid="hero-up-to-date"]' },
  // The same move on the desktop app.
  { scene: 'desktop-move', anchor: '[data-testid="desktop-channel-move-pending"]' },
]

await mkdir(outDir, { recursive: true })
const b = await chromium.launch()
const ctx = await b.newContext({ viewport: { width: 900, height: 900 }, deviceScaleFactor: 2 })
for (const { scene, anchor } of SCENES) {
  const p = await ctx.newPage()
  await p.goto(`${base}/capture/channel-move-pending.html?scene=${scene}&theme=dark`, { waitUntil: 'networkidle' })
  await p.locator(anchor).first().waitFor({ state: 'visible', timeout: 20_000 })
  const out = `${outDir}/${scene}.png`
  await p.screenshot({ path: out, fullPage: true })
  console.log(`captured ${out} (${anchor} asserted)`)
  await p.close()
}
await b.close()
