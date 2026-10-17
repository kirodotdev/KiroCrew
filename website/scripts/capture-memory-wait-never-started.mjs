/**
 * Frames of the memory-wait copy, one reason end to end: the queued chip under
 * macOS pressure, then a wave a memory wait ended, on the chat's run card and in
 * the Subagents panel.
 *
 * Asserts before writing each file: the chip ends "they never start if memory
 * stays short for 30 minutes", the card gives each reason its own note, and the
 * panel heads each run "Never started" and names "Retry failed".
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort   # in another shell
 *   node scripts/capture-memory-wait-never-started.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/memory-wait-never-started'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 444, height: 900 }, deviceScaleFactor: 2 })
let failed = false

const check = (label, ok, detail) => {
  console.log(`${label}: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify(detail)}`)
  if (!ok) failed = true
  return ok
}

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/memory-wait-never-started.html?scene=chip&theme=${theme}`)
  const banner = page.getByTestId('subagent-queued-banner')
  await banner.waitFor()
  const chip = (await banner.innerText()).replace(/\s+/g, ' ')
  if (check(`${theme} chip`, chip.includes('they never start if memory stays short for 30 minutes') && !chip.includes('they start once'), chip)) {
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/chip-${theme}.png` })
  }

  await page.goto(`${BASE}/capture/memory-wait-never-started.html?scene=ended&theme=${theme}`)
  await page.getByTestId('subagent-run-card').waitFor()
  // Expand each panel card so its notice shows.
  const headers = page.locator('[data-capture-panel] [role="button"][aria-expanded="false"]')
  for (let i = (await headers.count()) - 1; i >= 0; i--) await headers.nth(i).click()
  await page.waitForTimeout(300)
  const notes = await page.getByTestId('subagent-card-never-started-reason').allInnerTexts()
  const panel = await page.locator('[data-capture-panel]').innerText()
  const ok = notes.length === 2
    && notes[0].includes('1 never started — macOS memory pressure')
    && notes[1].includes('2 never started — not enough memory freed up in time')
    && panel.includes('Retry failed (3)')
    && panel.includes('Never started')
    && !/\bError\b/.test(panel)
    && panel.includes('Not enough memory freed up in time; free up memory, then use “Retry failed”')
    && !panel.includes('retry once memory frees up')
  if (check(`${theme} ended`, ok, { notes })) {
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/ended-${theme}.png` })
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
