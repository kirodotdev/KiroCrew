/**
 * Screenshots of the collapsible tool group's approval row while the group is
 * expanded (#5487).
 *
 * Drives the isolated capture entry (website/capture/approval-row-expanded-group.html),
 * which mounts the REAL CollapsibleToolGroup in the auto-expanded pending state.
 *
 * Each frame ASSERTS the state before writing the file, so a frame cannot
 * silently document the wrong state:
 *   after  (default): the group is expanded (aria-expanded=true) AND the
 *     approval row is present — Approve / Trust / Reject all reachable.
 *     This assertion FAILS on the pre-fix code, so an after-frame cannot be
 *     shot from the old component by mistake.
 *   --before: the group is expanded but NO approval buttons render — the
 *     dead end this PR removes (shot from the pre-fix checkout).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort   # in another shell
 *   node scripts/capture-approval-row-expanded-group.mjs http://127.0.0.1:6823 ../temp-screenshots/approval-row-expanded-group-5487 [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/approval-row-expanded-group-5487'
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'approval-row-expanded-dark', theme: 'dark' },
  { name: 'approval-row-expanded-light', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 600, height: 320 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/approval-row-expanded-group.html?theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  // The header exists in both states; wait for it, then check its disclosure.
  const header = page.locator('[data-capture-root] button[aria-expanded]')
  await header.waitFor()
  const expanded = (await header.getAttribute('aria-expanded')) === 'true'

  const labels = (await page
    .locator('[data-capture-root] button', { hasText: /Approve|Trust|Reject/ })
    .allInnerTexts()).map(t => t.trim())
  // The diff ungated TWO blocks — the buttons and the command preview — so the
  // self-check must assert both or a frame could document a half-regression.
  const previews = await page.locator('[data-capture-root] pre').count()
  const ok = BEFORE
    ? expanded && labels.length === 0 && previews === 0 // before-frame: expanded pending group is a dead end
    : expanded && /Approve/.test(labels[0] || '') && labels.some(t => /Trust/.test(t)) && labels.some(t => /Reject/.test(t)) && previews === 1
  console.log(`${s.name}${BEFORE ? ' (before)' : ''}: expanded=${expanded} buttons=${JSON.stringify(labels)} previews=${previews} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  const suffix = BEFORE ? '-before' : ''
  await page.screenshot({ path: `${OUT}/${s.name}${suffix}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
