/**
 * Screenshots of the About hero's two version lanes (#11356), via the
 * capture/about-version-lanes harness (stubbed Electron bridge + dispatched
 * gateway status). Asserts the lane split actually rendered before shooting,
 * so a frame can never quietly show the old single badge.
 *
 * Usage: node scripts/capture-about-version-lanes.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://localhost:5199'
const out = process.argv[3] || '../temp-screenshots/about-version-lanes'

const b = await chromium.launch()
for (const [scene, theme] of [['differ', 'dark'], ['differ', 'light'], ['same', 'dark']]) {
  const p = await (await b.newContext({ viewport: { width: 820, height: 420 }, deviceScaleFactor: 2 })).newPage()
  await p.goto(`${base}/capture/about-version-lanes.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
  await p.locator('[data-testid="about-version"]').waitFor({ timeout: 20_000 })
  // `differ` must show the gateway line; `same` must show the chips in the
  // bottom row with no gateway line at all.
  if (scene === 'differ') await p.locator('[data-testid="about-gateway-lane"]').waitFor({ timeout: 20_000 })
  else await p.locator('text=2ed1f603d').waitFor({ timeout: 20_000 })
  if (scene === 'same' && await p.locator('[data-testid="about-gateway-lane"]').count()) {
    throw new Error('scene=same rendered a gateway lane — the equal-version fold regressed')
  }
  const hero = p.locator('[data-capture-root] > div > div').first()
  const file = `${out}/about-${scene}-${theme}.png`
  await hero.screenshot({ path: file })
  console.log(`captured ${file}`)
}
await b.close()
