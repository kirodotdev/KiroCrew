/**
 * Evidence for #25970: a cut-off line in the Files tab header, the folder panel
 * and the remote git history shows its full text in the shared bubble on
 * keyboard focus. Drives website/capture/truncated-text-25970.html.
 *
 * Usage (website/; starts its own loopback vite server on 6851 when no URL is given):
 *   MODE=after node scripts/capture-25970-truncated-text.mjs "" ../temp-screenshots/25970
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'

// No base URL given: serve the capture entry from a loopback-only vite server
// owned by this script, closed when it exits.
const server = process.argv[2] ? null : await createServer({ server: { host: '127.0.0.1', port: 6851, strictPort: true } })
if (server) await server.listen()
const BASE = process.argv[2] || 'http://127.0.0.1:6851'
const OUT = process.argv[3] || '../temp-screenshots/25970'
const MODE = process.env.MODE || 'after'
mkdirSync(OUT, { recursive: true })

const DIR = '/Users/me/workspace/customer-onboarding-service-migration-2026'
const SUBJECT = 'fix(billing): keep the invoice total and the tax line in step when a refund lands mid-cycle'
const VIEWS = [
  { view: 'files', target: 'customer-onboarding-service-migration-2026', full: DIR },
  { view: 'folder', shot: 'folder-header', target: 'customer-onboarding-service-migration-2026', full: DIR },
  { view: 'folder', target: DIR, full: DIR },
  { view: 'git', target: SUBJECT, full: SUBJECT },
]

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const fail = (label, msg) => { console.error(`[${label}] ${msg}`); failures++ }

for (const theme of ['dark', 'light']) {
  for (const { view, shot = view, target, full } of VIEWS) {
    const label = `${MODE}-${theme}-${shot}`
    const page = await browser.newPage({ viewport: { width: 520, height: 360 } })
    page.on('pageerror', e => fail(label, `pageerror: ${e.message}`))
    await page.goto(`${BASE}/capture/truncated-text-25970.html?theme=${theme}&view=${view}`, { waitUntil: 'networkidle' })
    const line = page.locator(`text="${target}" >> visible=true`).first()
    await line.waitFor({ timeout: 15000 })
    const cut = await line.evaluate(el => el.scrollWidth > el.clientWidth)
    if (!cut) fail(label, 'the line is not cut off, so the frame shows nothing')
    // Reach the line the way a keyboard user does: Tab until it holds focus.
    let reached = false
    for (let i = 0; i < 25 && !reached; i++) {
      await page.keyboard.press('Tab')
      reached = await line.evaluate(el => el === document.activeElement)
    }
    const tip = page.getByRole('tooltip')
    const shown = reached && await tip.isVisible() ? await tip.textContent() : ''
    if (MODE === 'after' && !(reached && shown?.includes(full))) fail(label, `focus=${reached} bubble=${JSON.stringify(shown)}`)
    if (MODE === 'before' && reached) fail(label, 'the line is a tab stop on the before build')
    await page.screenshot({ path: `${OUT}/${label}.png`, animations: 'disabled' })
    console.log(`[${label}] cut=${cut} tab-reached=${reached} bubble=${JSON.stringify(shown)}`)
    await page.close()
  }
}
await browser.close()
if (server) await server.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log(`done - evidence in ${OUT}`)
