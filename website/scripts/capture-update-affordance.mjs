/**
 * Screenshots of the update-capability affordances on a WHEEL install — the
 * shape that produced the reported bug (the modal offered an apply the gateway
 * refuses with 409, and the settings nudge dot never lit).
 *
 * Drives the isolated capture entry (website/capture/update-affordance.html),
 * which mounts the real components with the gateway status seeded through the
 * same `sseStatus` action the WebSocket push dispatches in production.
 *
 * Each scene asserts its own marker and the script EXITS NONZERO when one is
 * missing, so it can never quietly emit a screenshot of the wrong state.
 *
 * Usage: node scripts/capture-update-affordance.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6805'
const OUT = process.argv[3] || '../temp-screenshots/update-capability-contract'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  {
    scene: 'settings-dot',
    out: '01-settings-nudge-dot-wheel-install',
    // The repo's UI copy is lowercase; matching a capitalised label silently
    // fails the scene instead of catching a real regression.
    marker: '[role="status"][aria-label="update available"]',
    shot: '[data-capture-root]',
    viewport: { width: 900, height: 620 },
    note: 'nudge dot lights on a wheel install (previously only ever lit for the desktop app)',
  },
  {
    scene: 'wheel-command',
    out: '02-about-panel-manual-command',
    marker: '[data-testid="manual-update-command"]',
    shot: '[data-capture-root]',
    viewport: { width: 820, height: 900 },
    note: 'the honest affordance: the exact command to run, no button the gateway would refuse',
  },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const { scene, out, marker, shot, viewport, note } of SCENES) {
      const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto(`${BASE}/capture/update-affordance.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      try {
        await page.waitForSelector(marker, { timeout: 10000 })
      } catch {
        console.error(
          `  FAIL ${theme}/${scene}: ${marker} never rendered` +
            (errors.length ? ` (${errors[0]})` : ''),
        )
        failed += 1
        await ctx.close()
        continue
      }
      const target = await page.$(shot)
      await target.screenshot({ path: `${OUT}/${out}-${theme}.png` })
      console.log(`  ${theme}/${scene} -> ${note}`)
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) {
    console.error(`${failed} scene(s) failed`)
    process.exit(1)
  }
  console.log(`\nWrote ${SCENES.length * 2} shots to ${OUT}`)
}

run().catch(e => {
  console.error(e)
  process.exit(1)
})
