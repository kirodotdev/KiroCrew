/**
 * Screenshots of the components-1 batch of the ErrorNotice sweep.
 *
 * Drives the isolated capture entry (website/capture/error-notice-components-1.html),
 * which mounts the REAL CrewWebhookSection / CrewWakeSection / ManageAgentsFooter /
 * ExecutionsView with fetch stubbed to reject, so the frame shows the shipped
 * ErrorNotice + "Ask the agent" link exactly as a user with an unreachable
 * gateway sees it.
 *
 * Each "after" scene asserts the hand-off link's RENDERED TEXT before writing the
 * file, so a run can never emit a frame without the affordance under test. Pass
 * `--before` when running against the base branch (the same entry copied
 * there): the assertion is then inverted — the frame must show NO hand-off — so
 * a before/after pair cannot be two frames of the same build.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort   # in another shell
 *   node scripts/capture-error-notice-components-1.mjs http://127.0.0.1:6824 ../temp-screenshots/en-c1 [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const args = process.argv.slice(2)
const before = args.includes('--before')
const positional = args.filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6824'
const OUT = positional[1] || '../temp-screenshots/error-notice-components-1'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'webhooks-dark', scene: 'webhooks', theme: 'dark' },
  { name: 'wake-dark', scene: 'wake', theme: 'dark' },
  { name: 'footer-dark', scene: 'footer', theme: 'dark' },
  { name: 'executions-light', scene: 'executions', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 320 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/error-notice-components-1.html?scene=${s.scene}&theme=${s.theme}`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  // Let the rejected fetch settle into the error state.
  await page.getByRole('alert').first().waitFor({ timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(300)
  const n = await page.getByRole('button', { name: 'Ask the agent' }).count()
  console.log(`${s.name}${before ? ' (before)' : ''}: ${n} hand-off link(s) rendered`)
  if (before ? n !== 0 : n < 1) { failed = true; continue }
  const suffix = before ? '-before' : '-after'
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}${suffix}.png` })
}

await browser.close()
if (failed) {
  console.error(before
    ? 'a "before" scene rendered a hand-off link — this is not the base build'
    : 'a scene rendered no hand-off link — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
