/**
 * Screenshots of the spawn-approval card's action row (#5400).
 *
 * Drives the isolated capture entry (website/capture/spawn-approval-trust.html),
 * which mounts the REAL ActivityViewer with a pending spawn approval.
 *
 * Each frame ASSERTS the action row before writing the file, so a frame cannot
 * silently document the wrong state:
 *   after  (default): the row is exactly Approve / Reject — no Trust trigger.
 *     This assertion FAILS on the pre-fix code, so an after-frame cannot be
 *     shot from the old card by mistake.
 *   --before: the row still offers the Trust dropdown (shot from the pre-fix
 *     checkout, for the PR's before/after evidence).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6822 --strictPort   # in another shell
 *   node scripts/capture-spawn-approval-trust.mjs http://127.0.0.1:6822 ../temp-screenshots/spawn-approval-trust [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6822'
const OUT = process.argv[3] || '../temp-screenshots/spawn-approval-trust'
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'spawn-approval-dark', theme: 'dark' },
  { name: 'spawn-approval-light', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 420, height: 360 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/spawn-approval-trust.html?theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Approval Needed', { exact: true }).waitFor()

  const row = page.locator('[data-capture-root]')
  const buttons = await row.locator('button', { hasText: /Approve|Reject|Trust/ }).allInnerTexts()
  const labels = buttons.map(t => t.trim())
  const hasTrust = labels.some(t => /Trust/.test(t))
  const ok = BEFORE
    ? hasTrust // before-frame: the dishonest Trust tier is still offered
    : labels.length === 2 && /Approve/.test(labels[0]) && /Reject/.test(labels[1])
  console.log(`${s.name}${BEFORE ? ' (before)' : ''}: buttons=${JSON.stringify(labels)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  const suffix = BEFORE ? '-before' : ''
  await page.screenshot({ path: `${OUT}/${s.name}${suffix}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
