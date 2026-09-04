/**
 * Screenshots for the apps-3 batch of the error-state sweep
 * (capture/error-notice-apps-3.html): ops-mission-control / papyrus /
 * personal-shopper / pptx-maker / workflows failure surfaces before
 * (hand-written) and after (shared ErrorNotice), side by side.
 *
 * Self-checking: the AFTER column must render exactly six `role="alert"`
 * notices, expose the agent hand-off on the four surfaces that hold no draft
 * (ops dispatch, signals claim, sites save, workflow run tree) and none on the
 * two beside a draft (papyrus editor buffer, preferences input); the BEFORE
 * column must expose no hand-off at all — a screenshot of the wrong state is
 * worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6825 --strictPort    # in another shell
 *   node scripts/capture-error-notice-apps-3.mjs http://127.0.0.1:6825 ../temp-screenshots/error-notice-apps-3
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6825'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-apps-3'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1140, height: 720 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/error-notice-apps-3.html?theme=${theme}`)
  await page.getByTestId('scene').waitFor()
  await page.getByTestId('workflow-run-tree-error').waitFor()

  const after = page.locator('text=After (this branch)').locator('..')
  const alerts = await after.getByRole('alert').count()
  if (alerts !== 6) throw new Error(`expected 6 role=alert notices in the AFTER column, got ${alerts}`)
  const handoffs = await after.getByRole('button', { name: /Ask the agent/ }).count()
  if (handoffs !== 4) throw new Error(`expected 4 agent hand-offs (dispatch, claim, sites, run tree), got ${handoffs}`)
  const before = page.locator('text=Before (origin/main)').locator('..')
  const beforeHandoffs = await before.getByRole('button', { name: /Ask the agent/ }).count()
  if (beforeHandoffs !== 0) throw new Error(`BEFORE column leaked ${beforeHandoffs} hand-off button(s)`)

  await page.screenshot({ path: join(OUT, `before-after-${theme}.png`), fullPage: true })
  console.log(`captured before-after-${theme}.png`)
}

await browser.close()
console.log(`done → ${OUT}`)
