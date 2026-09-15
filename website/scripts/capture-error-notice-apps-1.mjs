/**
 * Screenshots for the apps-1 batch of the error-state sweep
 * (capture/error-notice-apps-1.html): the code-review-sage failure surfaces
 * before (hand-written) and after (shared ErrorNotice), side by side.
 *
 * Self-checking: the AFTER column must expose the agent hand-off on every
 * surface that holds no draft (3 "Ask the agent" buttons), exactly one notice
 * without it (the paste-links textarea, No hand-off), the retry beside the
 * failed run, and every notice as `role="alert"` — a screenshot of the wrong
 * state is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort    # in another shell
 *   node scripts/capture-error-notice-apps-1.mjs http://127.0.0.1:6823 ../temp-screenshots/error-notice-apps-1
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-apps-1'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1140, height: 640 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/error-notice-apps-1.html?theme=${theme}`)
  await page.getByTestId('scene').waitFor()
  await page.getByRole('button', { name: /Run it again/ }).waitFor()

  const alerts = await page.getByRole('alert').count()
  if (alerts !== 4) throw new Error(`expected 4 role=alert notices in the AFTER column, got ${alerts}`)
  const handoffs = await page.getByRole('button', { name: /Ask the agent/ }).count()
  if (handoffs !== 3) throw new Error(`expected 3 agent hand-offs (FailureNotice, RunList, AgentSessionButton), got ${handoffs}`)
  // The BEFORE column must contain no hand-off and no shared notice.
  const beforeAlerts = await page.locator('text=Before (origin/main)').locator('..').getByRole('alert').count()
  if (beforeAlerts !== 0) throw new Error(`BEFORE column leaked ${beforeAlerts} role=alert notice(s)`)

  await page.screenshot({ path: join(OUT, `before-after-${theme}.png`), fullPage: true })
  console.log(`captured before-after-${theme}.png`)
}

await browser.close()
console.log(`done → ${OUT}`)
