/**
 * Frames of the activity panel's two approval cards refusing a decision on an
 * approval that is already gone (#11180).
 *
 * Reuses the spawn-approval capture entry with `subagent=1&refuse=404`, so both
 * surfaces render and every approval POST answers 404.
 *
 * Asserts before writing each file: the after-frame assertion fails on the
 * pre-fix code, and `--before` inverts it.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort   # in another shell
 *   node scripts/capture-activityviewer-terminal-approval.mjs <baseUrl> <outDir> [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/activityviewer-terminal-approval'
const BEFORE = process.argv.includes('--before')
const EXPECTED = 'This approval has expired or was already decided'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 420, height: 560 }, deviceScaleFactor: 2 })
let failed = false

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/spawn-approval-trust.html?subagent=1&refuse=404&theme=${theme}`)
  const root = page.locator('[data-capture-root]')
  await page.getByText('Approval Needed', { exact: true }).waitFor()

  const approve = root.locator('button', { hasText: /^\s*Approve\s*$/ })
  const cards = await approve.count()
  for (let i = cards - 1; i >= 0; i--) await approve.nth(i).click()

  await root.getByRole('alert').first().waitFor()
  await page.waitForTimeout(400)
  const notices = await root.getByRole('alert').allInnerTexts()
  const live = await root.locator('button', { hasText: /^\s*(Approve|Reject)\s*$/ }).count()

  const terminal = notices.length === cards && notices.every(t => t.includes(EXPECTED))
  const ok = cards === 2 && (BEFORE ? !terminal && live === 4 : terminal && live === 0)
  console.log(`${theme}${BEFORE ? ' (before)' : ''}: cards=${cards} live=${live} notices=${JSON.stringify(notices)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  await page.screenshot({ path: `${OUT}/terminal-approval-${theme}${BEFORE ? '-before' : ''}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
