/**
 * Frames of a spawn approval that is already gone, refused through the
 * composer's spawn banner while the activity panel shows the same sub-agent.
 *
 * Asserts before writing each file: after the Approve click the banner must be
 * withdrawn with the "no longer pending" notice, and the panel card must drop
 * its buttons for the same approval. `--before` inverts it (the banner keeps
 * its buttons, and the panel still offers the decision).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort   # in another shell
 *   node scripts/capture-spawn-approval-gone.mjs <baseUrl> <outDir> [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/spawn-approval-gone'
const BEFORE = process.argv.includes('--before')
const EXPECTED = 'This approval has expired or was already decided'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1040, height: 560 }, deviceScaleFactor: 2, locale: 'en-US' })
let failed = false

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/spawn-approval-gone.html?theme=${theme}`, { waitUntil: 'domcontentloaded', timeout: 120000 })
  const composer = page.locator('[data-capture-composer]')
  const panel = page.locator('[data-capture-panel]')
  await composer.getByText(/awaiting your approval to run/).waitFor()

  await composer.locator('button', { hasText: /^\s*Approve\s*$/ }).click()
  await page.waitForTimeout(600)

  const banner = await composer.getByText(/awaiting your approval to run/).count()
  // The refusal is an error, so the composer shows it through ErrorNotice.
  const status = (await composer.getByRole('alert').allInnerTexts()).join(' ')
  const panelLive = await panel.locator('button', { hasText: /^\s*(Approve|Reject)\s*$/ }).count()
  const panelNotice = (await panel.getByRole('alert').allInnerTexts()).join(' ')

  const after = banner === 0 && status.includes(EXPECTED) && panelLive === 0 && panelNotice.includes(EXPECTED)
  const before = banner === 1 && !status.includes(EXPECTED) && panelLive === 2
  const ok = BEFORE ? before : after
  console.log(`${theme}${BEFORE ? ' (before)' : ''}: banner=${banner} status=${JSON.stringify(status)} panelLive=${panelLive} panelNotice=${JSON.stringify(panelNotice)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  await page.screenshot({ path: `${OUT}/spawn-approval-gone-${theme}${BEFORE ? '-before' : ''}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
