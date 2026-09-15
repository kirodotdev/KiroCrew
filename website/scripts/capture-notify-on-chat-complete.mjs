/**
 * Screenshots of the "notify when a background chat finishes" toggle
 * (capture/notify-on-chat-complete.html) — the new Desktop alerts section this
 * PR adds to Settings > Notifications.
 *
 * Self-checking: asserts the section title and toggle label really rendered
 * (i18n initialised, real components mounted) and that the two panes really are
 * the OFF and ON states — then screenshots. A screenshot of a blank or
 * single-state frame is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort    # in another shell
 *   node scripts/capture-notify-on-chat-complete.mjs http://127.0.0.1:6842 ../temp-screenshots/notify-on-chat-complete
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/notify-on-chat-complete'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1040, height: 420 }, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/notify-on-chat-complete.html?theme=${theme}`)

  // Both panes must have mounted their toggle. The section title and label come
  // from i18n; waiting for two switch roles proves both panes rendered and that
  // i18n resolved (a blank label would still be a switch, but the title check
  // below fails loudly if i18n did not initialise).
  await page.waitForFunction(() => document.querySelectorAll('[role="switch"]').length === 2)

  const switches = page.locator('[role="switch"]')
  const checked = await switches.evaluateAll((els) => els.map((e) => e.getAttribute('aria-checked')))
  // Left pane OFF, right pane ON — the comparison IS the evidence, so a frame
  // where both read the same would prove nothing about the control.
  if (checked[0] !== 'false') throw new Error(`left pane not OFF, aria-checked=${checked[0]}`)
  if (checked[1] !== 'true') throw new Error(`right pane not ON, aria-checked=${checked[1]}`)

  // The section title and toggle label must be the real i18n strings, not blank
  // — a blank frame is the classic "captured before i18n initialised" failure.
  if ((await page.getByText('Desktop alerts', { exact: false }).count()) < 1) {
    throw new Error('section title "Desktop alerts" did not render; i18n likely uninitialised')
  }
  if ((await page.getByText(/background chat finishes/i).count()) < 2) {
    throw new Error('toggle label missing from one or both panes')
  }

  await page.screenshot({ path: join(OUT, `notify-on-chat-complete-${theme}.png`) })
  console.log(`captured notify-on-chat-complete-${theme}.png`)
}

await browser.close()
console.log(`done → ${OUT}`)
