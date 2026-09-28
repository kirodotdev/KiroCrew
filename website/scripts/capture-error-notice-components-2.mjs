/**
 * Screenshots for the components-2 batch of the error-state sweep
 * (capture/error-notice-components-2.html): shared-component failure surfaces
 * before (hand-written / silent / alert()) and after (shared ErrorNotice),
 * side by side.
 *
 * Self-checking, across BOTH rows: the AFTER columns must render every surface
 * as `role="alert"` (24 notices), expose the agent hand-off on exactly the
 * surfaces that hold no draft (15 "Ask the agent" buttons), and keep the Retry
 * beside the IssuePanel notice and the plain remedy line under the
 * KiroPrerequisiteGate notice. The BEFORE columns reconstruct the origin/main
 * markup faithfully (several of those surfaces DID carry a bare role="alert"),
 * so their alerts are counted separately and must carry no hand-off — a
 * screenshot of the wrong state is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6827 --strictPort    # in another shell
 *   node scripts/capture-error-notice-components-2.mjs http://127.0.0.1:6827 ../temp-screenshots/error-notice-components-2
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6827'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-components-2'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1140, height: 900 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/error-notice-components-2.html?theme=${theme}`)
  await page.getByTestId('scene').waitFor()
  await page.getByTestId('after-folder').waitFor()

  const after = page.locator('[data-col="after"]')
  const before = page.locator('[data-col="before"]')

  const alerts = await after.getByRole('alert').count()
  if (alerts !== 24) throw new Error(`expected 24 role=alert notices in the AFTER columns, got ${alerts}`)
  const handoffs = await after.getByRole('button', { name: /Ask the agent/ }).count()
  if (handoffs !== 15) throw new Error(`expected 15 agent hand-offs in the AFTER columns, got ${handoffs}`)
  const retries = await after.getByRole('button', { name: /Retry/ }).count()
  if (retries !== 1) throw new Error(`expected the IssuePanel Retry beside its notice, got ${retries}`)
  const remedy = await after.getByText(/Fix the cause named above/).count()
  if (remedy !== 1) throw new Error(`expected the KiroPrerequisiteGate remedy line under its notice, got ${remedy}`)
  const beforeHandoffs = await before.getByRole('button', { name: /Ask the agent/ }).count()
  if (beforeHandoffs !== 0) throw new Error(`BEFORE column leaked ${beforeHandoffs} hand-off button(s)`)

  await page.screenshot({ path: join(OUT, `before-after-${theme}.png`), fullPage: true })
  console.log(`captured before-after-${theme}.png`)
}

await browser.close()
console.log(`done → ${OUT}`)
