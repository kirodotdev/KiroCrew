/**
 * Capture the Computer Use settings panel on a Windows host, for PR evidence.
 *
 * The badge under test is data-driven: it reads `platform` + `supported` from
 * `GET /api/computer-use/config`, so a screenshot only proves anything if the
 * gateway it points at is actually reporting `platform: "windows"` — which is why
 * this script takes a live URL rather than stubbing the response.
 *
 * Usage:
 *   node scripts/capture-computer-use-windows.mjs <dashboard-url-with-token> <out-dir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const url = process.argv[2]
const outDir = process.argv[3] ?? '.'
if (!url) {
  console.error('usage: capture-computer-use-windows.mjs <url> <out-dir>')
  process.exit(2)
}
mkdirSync(outDir, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })

page.on('console', (msg) => {
  if (msg.type() === 'error') console.log('  [console error]', msg.text().slice(0, 200))
})

// The token in the URL mints the session cookie, exactly as a real browser visit does.
await page.goto(url, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(3000)

// A fresh data home shows the first-run import wizard over everything, so dismiss
// it before navigating. Skipping (not completing) it keeps the capture free of any
// imported state — the panel under test reads only the keystone and the backend.
// It is a MULTI-step wizard, so one dismissal only advances a page. Loop until no
// wizard control is left, bounded so a wizard that never finishes cannot hang the
// capture. Skipping rather than completing keeps the capture free of imported state.
for (let step = 0; step < 8; step += 1) {
  let clicked = false
  for (const label of ['Skip all', 'Skip import', 'Continue', 'Done', 'Finish', 'Skip']) {
    const btn = page.getByRole('button', { name: label, exact: true }).first()
    if (await btn.count()) {
      await btn.click().catch(() => {})
      await page.waitForTimeout(1200)
      clicked = true
      break
    }
  }
  if (!clicked) break
}

// Reach Settings by CLICKING the nav, not by rewriting the URL: the dashboard is a
// single-page app whose router does not read a hash route, so a goto() lands back on
// Sessions and the capture silently photographs the wrong page.
await page.getByText('Settings', { exact: true }).first().click()
await page.waitForTimeout(3000)

// Settings is itself tabbed, so landing on it shows Overview. The panel under test
// lives behind its own SYSTEM > Computer Use tab.
await page.getByText('Computer Use', { exact: true }).first().click()
await page.waitForTimeout(3500)

// Wait for the BADGE rather than the heading: the heading text also matches the nav
// item, so it is present even when the panel has not rendered.
try {
  await page.getByText(/Focus \+ cursor|macOS only/).first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(1000)
} catch {
  console.log('  (platform badge not found — capturing the page as-is)')
}

// The badge text is the assertion this screenshot exists to carry.
for (const label of ['Focus + cursor', 'macOS only']) {
  const count = await page.getByText(label, { exact: true }).count()
  console.log(`  badge "${label}": ${count} node(s)`)
}

await page.screenshot({ path: join(outDir, '03-settings-panel-windows.png'), fullPage: false })
console.log('  wrote 03-settings-panel-windows.png')

// The section on its own, which is the part a reviewer needs to read.
const section = page.locator('section', { hasText: 'Computer Use' }).first()
if (await section.count()) {
  await section.screenshot({ path: join(outDir, '04-settings-section-windows.png') })
  console.log('  wrote 04-settings-section-windows.png')
}

await browser.close()
