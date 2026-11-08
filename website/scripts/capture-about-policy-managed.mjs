/**
 * Screenshots of the About panel's policy-managed notice (GH #15797), via the
 * capture/about-policy-managed harness. Asserts the notice element is present
 * before shooting the `policy` scene.
 *
 * Usage: node scripts/capture-about-policy-managed.mjs <viteBase> <outDir>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://localhost:5199'
const out = process.argv[3] || '../temp-screenshots/about-policy-managed'

const b = await chromium.launch()
const scenes = [
  ['policy', 'dark'],
  ['policy', 'light'],
  ['normal', 'dark'],
  ['reused-gateway', 'dark'],
]
for (const [scene, theme] of scenes) {
  const ctx = await b.newContext({ viewport: { width: 820, height: 480 }, deviceScaleFactor: 2 })
  const p = await ctx.newPage()
  await p.goto(`${base}/capture/about-policy-managed.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
  await p.locator('[data-capture-root]').waitFor({ timeout: 20_000 })
  if (scene === 'policy') {
    // The managed-by-policy notice must be visible before shooting.
    await p.locator('[data-testid="gateway-policy-managed-note"]').waitFor({ timeout: 20_000 })
  } else if (scene === 'reused-gateway') {
    // The reused-gateway scene must NOT show the policy notice.
    const count = await p.locator('[data-testid="gateway-policy-managed-note"]').count()
    if (count > 0) throw new Error('scene=reused-gateway rendered a policy-managed notice — regression')
    // Wait for the about panel to settle.
    await p.locator('[data-testid="about-version"]').waitFor({ timeout: 20_000 })
  } else {
    // The normal scene must NOT show the notice.
    const count = await p.locator('[data-testid="gateway-policy-managed-note"]').count()
    if (count > 0) throw new Error('scene=normal rendered a policy-managed notice — regression')
    // Wait for the about panel to settle (version chip present).
    await p.locator('[data-testid="about-version"]').waitFor({ timeout: 20_000 })
  }
  const root = p.locator('[data-capture-root] > div').first()
  const file = `${out}/about-${scene}-${theme}.png`
  await root.screenshot({ path: file })
  console.log(`captured ${file}`)
  await ctx.close()
}
await b.close()
