/**
 * Screenshots of the Spec Builder decision card's pending / locked states.
 *
 * Drives the ISOLATED capture entry (website/capture/spec-decisions.html) — see
 * its header for why the full SPA is not booted here.
 *
 * Every scene asserts a marker and the script EXITS NONZERO if one is missing,
 * so it cannot quietly emit a screenshot of the wrong state. The `locked` and
 * `clicked` scenes additionally assert that NO option control is present, which
 * is the whole claim of the change.
 *
 * Usage: node scripts/capture-spec-decisions.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6805'
const OUT = process.argv[3] || '../temp-screenshots/spec-decision-lock'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { scene: 'pending', options: true, note: 'unanswered — options offered' },
  { scene: 'busy', options: true, disabled: true, note: 'agent working — options held back' },
  { scene: 'clicked', options: false, note: 'optimistic lock, before any agent write' },
  { scene: 'locked', options: false, note: 'recorded by the backend — re-emitted pending card stays settled' },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const { scene, options, disabled, note } of SCENES) {
      const ctx = await browser.newContext({
        viewport: { width: 460, height: 420 },
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', (e) => errors.push(e.message))
      await page.goto(`${BASE}/capture/spec-decisions.html?scene=${scene}&theme=${theme}`, {
        waitUntil: 'networkidle',
      })
      try {
        await page.waitForSelector('text=DECISIONS', { timeout: 10000 })
        if (scene === 'clicked') {
          // Driven from here rather than from the entry: Playwright waits for the
          // control to exist, where a rAF callback fires before React has painted
          // the tree and clicks nothing.
          await page.locator('[role="button"]').filter({ hasText: 'Hosted HTTPS listener' }).click()
          await page.waitForFunction(
            () => document.querySelectorAll('[role="button"]').length === 0,
            null,
            { timeout: 5000 },
          )
        }
        const optionCount = await page.locator('[role="button"]').count()
        if (options && optionCount === 0) throw new Error('no option controls rendered')
        if (!options && optionCount !== 0) {
          throw new Error(`${optionCount} option control(s) still clickable`)
        }
        if (disabled) {
          // The claim of this scene: present but not answerable.
          const enabled = await page.locator('[role="button"]:not([aria-disabled="true"])').count()
          if (enabled !== 0) throw new Error(`${enabled} option control(s) still enabled`)
        }
      } catch (e) {
        console.error(`  FAIL ${theme}/${scene}: ${e.message}` + (errors.length ? ` (${errors[0]})` : ''))
        failed += 1
        await ctx.close()
        continue
      }
      const target = await page.$('[data-capture-root]')
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

run().catch((e) => {
  console.error(e)
  process.exit(1)
})
