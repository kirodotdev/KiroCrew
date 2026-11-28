/**
 * Screenshots of the update popup when the staged version and the offered
 * version differ, via the capture/update-staged-newer harness. Each frame is
 * asserted before it is shot, so a frame can never quietly show the other state.
 * The About card is not shot: it already treated only the offered version as
 * installed, so it looks the same on both sides.
 *
 * Usage: node scripts/capture-update-staged-newer.mjs <viteBase> <outDir> [after|before]
 *   after  (default): the branch under review. The popup keeps the offer's install
 *          action whether the staged version is older or newer than the offer.
 *   before: the base commit, served from its own checkout. The popup offers
 *          Restart in both states.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://localhost:5199'
const out = process.argv[3] || '../temp-screenshots/update-staged-newer'
const side = process.argv[4] === 'before' ? 'before' : 'after'
mkdirSync(out, { recursive: true })

const b = await chromium.launch()
const shots = [
  ['dialog', 'staged-older', 'dark'],
  ['dialog', 'staged-newer', 'dark'],
]
for (const [surface, scene, theme] of shots) {
  const p = await (await b.newContext({ viewport: { width: 900, height: 560 }, deviceScaleFactor: 2 })).newPage()
  await p.goto(`${base}/capture/update-staged-newer.html?surface=${surface}&scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
  await p.locator('[role="dialog"]').waitFor({ timeout: 20_000 })
  const want = side === 'after' ? 'Update when I restart' : 'Restart to update'
  await p.locator('[role="dialog"]').getByRole('button', { name: want }).waitFor({ timeout: 20_000 })
  const file = `${out}/${surface}-${scene}-${theme}-${side}.png`
  await p.screenshot({ path: file })
  console.log(`captured ${file}`)
}
await b.close()
