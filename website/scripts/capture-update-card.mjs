/**
 * Screenshots of the update card's download states (issues #735 / #737).
 *
 * Drives the ISOLATED capture entry (website/capture/update-card.html), which
 * mounts AboutPanel against the real stylesheet + theme variables, with the
 * update state seeded into the same ['update-state'] query cache that
 * useUpdateSubscription writes in production.
 *
 * Why not the full SPA: the app shell needs a dozen /api fixtures plus live
 * websocket frames to boot, and a half-stubbed shell renders its ERROR BOUNDARY
 * instead of the page -- a screenshot of the wrong thing is worse evidence than
 * none. The tradeoff is that the surrounding settings chrome is not captured,
 * which is acceptable because this change is confined to the card.
 *
 * Every scene asserts its own marker and the script EXITS NONZERO if one is
 * missing, so it can never quietly emit a screenshot of the wrong state.
 *
 * Usage: node scripts/capture-update-card.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6805'
const OUT = process.argv[3] || '../temp-screenshots/updater-download-states'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  {
    scene: 'found',
    marker: '[data-testid="update-card"]',
    note: 'discovered -- consent gate holds, nothing downloaded',
  },
  {
    scene: 'downloading',
    marker: '[data-testid="update-progress"]',
    note: 'determinate bar + percent/rate (#737)',
  },
  {
    scene: 'downloaded',
    marker: '[data-testid="update-manual-fallback"]',
    note: 'staged + manual reinstall escape hatch',
  },
  {
    scene: 'download-failed',
    marker: '[data-testid="update-download-error"]',
    note: 'card SURVIVES with a retry (#735/#736)',
  },
  {
    scene: 'install-failed',
    marker: '[data-testid="update-manual-fallback"]',
    note: 'install failure keeps the card + escape hatch (UX finding)',
  },
  {
    scene: 'check-failed',
    marker: 'text=/couldn.t check for updates/i',
    note: 'a check failure stays a status line',
  },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const { scene, marker, note } of SCENES) {
      const ctx = await browser.newContext({
        viewport: { width: 820, height: 620 },
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto(`${BASE}/capture/update-card.html?scene=${scene}&theme=${theme}`, {
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
      // Prefer the card itself; the check-failure scene has no card, so fall
      // back to the padded root so the status line is still framed.
      const target =
        (await page.$('[data-testid="update-card"]')) || (await page.$('[data-capture-root]'))
      await target.screenshot({ path: `${OUT}/${theme}-${scene}.png` })
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
