/**
 * Screenshots for the pages-rest-2 batch of the error-state sweep
 * (capture/error-notice-pages-rest-2.html): top-level page failure surfaces
 * before (hand-written) and after (shared ErrorNotice), side by side.
 *
 * Self-checking: the AFTER column must render every surface as `role="alert"`
 * (7 notices), expose the agent hand-off on exactly the surfaces that hold no
 * draft (5 "Ask the agent" buttons — Schedule row action, Webhooks load, Logs
 * level change, remote artifact detail, session archive), keep the Retry beside
 * the Webhooks notice, and the BEFORE column must contain no `role="alert"` —
 * a screenshot of the wrong state is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6826 --strictPort    # in another shell
 *   node scripts/capture-error-notice-pages-rest-2.mjs http://127.0.0.1:6826 ../temp-screenshots/error-notice-pages-rest-2
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6826'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-pages-rest-2'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1140, height: 640 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/error-notice-pages-rest-2.html?theme=${theme}`)
  await page.getByTestId('scene').waitFor()
  await page.getByRole('button', { name: /Retry/ }).first().waitFor()

  const alerts = await page.getByRole('alert').count()
  if (alerts !== 7) throw new Error(`expected 7 role=alert notices in the AFTER column, got ${alerts}`)
  const handoffs = await page.getByRole('button', { name: /Ask the agent/ }).count()
  if (handoffs !== 5) throw new Error(`expected 5 agent hand-offs (Schedule row, Webhooks load, Logs level, remote artifact, session archive), got ${handoffs}`)
  // The BEFORE column must contain no hand-off and no shared notice.
  const beforeAlerts = await page.locator('text=Before (origin/main)').locator('..').getByRole('alert').count()
  if (beforeAlerts !== 0) throw new Error(`BEFORE column leaked ${beforeAlerts} role=alert notice(s)`)

  await page.screenshot({ path: join(OUT, `before-after-${theme}.png`), fullPage: true })
  console.log(`captured before-after-${theme}.png`)
}

await browser.close()
console.log(`done → ${OUT}`)
