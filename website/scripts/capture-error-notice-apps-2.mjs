/**
 * Screenshots for the apps-2 batch of the error-state sweep
 * (capture/error-notice-apps-2.html): issue-radar / md-notebook / meetings
 * failure surfaces before (hand-written) and after (shared ErrorNotice), side
 * by side.
 *
 * Self-checking: the AFTER column must expose the agent hand-off on every
 * surface that holds no draft (4 "Ask the agent" buttons), exactly one notice
 * without it (LabelPicker inside the repo settings form, No hand-off), the
 * retry beside the failed summary, and every notice as `role="alert"` — a
 * screenshot of the wrong state is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort    # in another shell
 *   node scripts/capture-error-notice-apps-2.mjs http://127.0.0.1:6824 ../temp-screenshots/error-notice-apps-2
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-apps-2'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1140, height: 640 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/error-notice-apps-2.html?theme=${theme}`)
  await page.getByTestId('scene').waitFor()
  await page.getByRole('button', { name: /Retry/ }).first().waitFor()

  const alerts = await page.getByRole('alert').count()
  if (alerts !== 5) throw new Error(`expected 5 role=alert notices in the AFTER column, got ${alerts}`)
  const handoffs = await page.getByRole('button', { name: /Ask the agent/ }).count()
  if (handoffs !== 4) throw new Error(`expected 4 agent hand-offs (AiSummaryCard, CrewPageView, PrActionsBar, MeetingsPage), got ${handoffs}`)
  // The BEFORE column must contain no hand-off and no shared notice.
  const beforeAlerts = await page.locator('text=Before (origin/main)').locator('..').getByRole('alert').count()
  if (beforeAlerts !== 0) throw new Error(`BEFORE column leaked ${beforeAlerts} role=alert notice(s)`)

  await page.screenshot({ path: join(OUT, `before-after-${theme}.png`), fullPage: true })
  console.log(`captured before-after-${theme}.png`)
}

await browser.close()
console.log(`done → ${OUT}`)
