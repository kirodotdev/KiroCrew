/**
 * Screenshot of Mochi's approval card when the gateway proves a SESSION grant but
 * no command scope.
 *
 * Drives website/capture/mochi-scopeless-trust.html, which mounts the REAL
 * ChatPanel Bubble. The frame ASSERTS its state before writing the file:
 *   - the Trust control names the session scope, and the bare verb "Trust" is
 *     absent (with no tiers to reveal, the click IS the grant),
 *   - the control carries no `aria-expanded`, since it discloses nothing,
 *   - the hint under it describes the session and names no tool.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6844 --strictPort   # in another shell
 *   node scripts/capture-mochi-scopeless-trust.mjs http://127.0.0.1:6844 ../temp-screenshots/mochi-scopeless-trust
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6844'
const OUT = process.argv[3] || '../temp-screenshots/mochi-scopeless-trust'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 460, height: 280 }, deviceScaleFactor: 2 })

await page.goto(`${BASE}/capture/mochi-scopeless-trust.html`)
await page.waitForSelector('[data-capture-root]')

const scoped = page.locator('[data-capture-root] button', {
  hasText: 'Trust all tools for this session',
})
await scoped.first().waitFor()
const labels = (await page.locator('[data-capture-root] button').allInnerTexts()).map(t => t.trim())
const expanded = await scoped.first().getAttribute('aria-expanded')
const body = await page.locator('[data-capture-root]').innerText()

const ok =
  labels.includes('Trust all tools for this session')
  && !labels.includes('Trust')
  && expanded === null
  && /every tool for the rest of this session/i.test(body)
  && !/from now on/i.test(body)
console.log(
  `mochi-scopeless-trust: buttons=${JSON.stringify(labels)} expanded=${expanded} ${ok ? 'OK' : 'MISMATCH'}`,
)
if (ok) await page.screenshot({ path: `${OUT}/mochi-scopeless-trust-dark.png` })

await browser.close()
process.exit(ok ? 0 : 1)
