/**
 * Screenshots of the error-to-agent hand-off on migrated error surfaces.
 *
 * Drives the isolated capture entry (website/capture/error-handoff.html),
 * which mounts the REAL JobLogsView / ExecutionsView with fetch stubbed to
 * reject, so the frame shows the shipped ErrorNotice + "Ask the agent" link
 * exactly as a user with an unreachable gateway sees it.
 *
 * Each scene asserts the hand-off link's RENDERED TEXT before writing the
 * file, so a run can never emit a frame without the affordance under test.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort   # in another shell
 *   node scripts/capture-error-handoff.mjs http://127.0.0.1:6823 ../temp-screenshots/error-handoff
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/error-handoff'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'joblogs-dark', scene: 'joblogs', theme: 'dark' },
  { name: 'joblogs-light', scene: 'joblogs', theme: 'light' },
  { name: 'executions-dark', scene: 'executions', theme: 'dark' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 360 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/error-handoff.html?scene=${s.scene}&theme=${s.theme}`)
  await page.addStyleTag({
    content: '*, *::before, *::after { animation-duration: 0s !important;'
      + ' animation-delay: 0s !important; transition-duration: 0s !important;'
      + ' transition-delay: 0s !important; }',
  })
  await page.waitForSelector('[data-capture-root]')
  // The state under test: the error settled AND the hand-off is offered.
  const links = page.getByRole('button', { name: 'Ask the agent' })
  await links.first().waitFor({ timeout: 5000 })
  const n = await links.count()
  console.log(`${s.name}: ${n} hand-off link(s) rendered`)
  if (n < 1) { failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) {
  console.error('a scene rendered no hand-off link — no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
