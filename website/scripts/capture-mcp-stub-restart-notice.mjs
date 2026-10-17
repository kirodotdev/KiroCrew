/**
 * Screenshots of the restart-required notice on MCP Management -> Servers.
 *
 * Drives the isolated capture entry (website/capture/mcp-stub-restart-notice.html),
 * which mounts the REAL McpManagement page against the real stylesheet, theme
 * tokens and live i18n catalog. Not the full SPA: reaching this row in the shell
 * needs a configured MCP fleet and a live gateway, and a half-stubbed shell
 * screenshots its error boundary instead -- worse evidence than none.
 *
 * Each scene asserts its RENDERED STATE before writing the file, so a run can
 * never emit a frame that contradicts the diff. The after-scenes additionally
 * assert role=status and the ABSENCE of role=alert, which is the property the
 * change turns on: a pending restart must not be painted as a failure.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-mcp-stub-restart-notice.mjs http://127.0.0.1:6821 ../temp-screenshots/mcp-stub-restart-notice
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/mcp-stub-restart-notice'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'before-dark', theme: 'dark', click: false },
  { name: 'restart-required-dark', theme: 'dark', click: true },
  { name: 'restart-required-light', theme: 'light', click: true },
]

const EXPECTED = 'Restart the gateway for this to take effect'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1060, height: 760 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/mcp-stub-restart-notice.html?theme=${s.theme}`)
  await page.waitForSelector('[data-capture-root]')
  const toggle = page.getByRole('switch', { name: /alpha-mcp/i })
  await toggle.waitFor()

  if (s.click) {
    await toggle.click()
    await page.waitForSelector('[role="status"]')
  }

  const statusCount = await page.locator('[role="status"]').count()
  const alertCount = await page.locator('[role="alert"]').count()
  const text = statusCount ? (await page.locator('[role="status"]').first().innerText()).trim() : ''
  const checked = await toggle.getAttribute('aria-checked')

  // A pending restart is information, never a fault: role=status present and
  // role=alert absent is the assertion, not just "some banner appeared". The
  // switch must also read ON -- it shows the STORED allowlist, and a frame with
  // it snapped back off beside "Saved" would document the opposite of the fix.
  const ok = s.click
    ? statusCount === 1 && alertCount === 0 && text.includes(EXPECTED) && checked === 'true'
    : statusCount === 0 && alertCount === 0 && checked === 'false'

  console.log(`${s.name}: status=${statusCount} alert=${alertCount} switch=${checked} text=${JSON.stringify(text)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
}

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected state -- no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
