/**
 * Screenshot evidence for the Code Review Sage label filter (issue #3788).
 *
 * Two frames from the `capture/sage-label-filter` scene, which mounts the REAL
 * PrPickList behind the REAL SageProvider and stubs only `fetch`:
 *
 *   01-picker-open.png     the label menu open, one item per label with counts
 *   02-two-labels-or.png   two labels selected, queue narrowed by their OR
 *
 * The second frame is produced by CLICKING the real menu items and then
 * asserting a reduced row count -- so the frame is
 * evidence of the shipped wiring rather than of a hand-posed component. If the
 * narrowing regresses, this script FAILS instead of quietly shooting a frame
 * that looks the same.
 *
 * Usage: node scripts/capture-sage-label-filter.mjs [outDir]
 * Boots its own Vite dev server on loopback, so it needs no running gateway.
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2]
  || fileURLToPath(new URL('../../temp-screenshots/3788-sage-label-filter/', import.meta.url))
mkdirSync(OUT, { recursive: true })

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const vite = await createServer({
  root: ROOT,
  configFile: join(ROOT, 'vite.config.ts'),
  // Loopback only, and let the OS pick the port so a concurrent harness on this
  // host cannot collide with us.
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await vite.listen()
const { port } = vite.httpServer.address()
const base = `http://127.0.0.1:${port}`

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const page = await (await browser.newContext({
  viewport: { width: 460, height: 760 },
  deviceScaleFactor: 2,
})).newPage()

const problems = []
page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`))
page.on('console', (m) => { if (m.type() === 'error') problems.push(`console: ${m.text()}`) })
// Name the URL. A bare "404 (Not Found)" in the console tells a reader nothing
// about whether the scene is broken or an unrelated asset is simply absent from
// the dev server, and an unattributed problem line is one nobody can act on.
page.on('response', (r) => { if (r.status() >= 400) problems.push(`HTTP ${r.status()} ${r.url()}`) })
page.on('requestfailed', (r) => problems.push(`requestfailed ${r.url()} ${r.failure()?.errorText}`))

await page.goto(`${base}/capture/sage-label-filter.html?theme=dark`, { waitUntil: 'networkidle' })

const rows = () => page.locator('button[aria-label^="Open pull request #"]')
const trigger = () => page.locator('button[aria-haspopup="menu"]').first()
const item = (name) => page.getByRole('menuitem').filter({ hasText: name }).first()

// The scene must have reached the state the frame is about, or the shot is a lie.
await rows().first().waitFor({ state: 'visible', timeout: 20_000 })
const total = await rows().count()
if (total !== 5) throw new Error(`expected the 5 seeded PRs, saw ${total}`)
// Open the menu for the resting frame: a collapsed trigger photographs as an
// unremarkable button, so the frame that shows what the feature IS is the open
// one, listing every label with its count.
await trigger().waitFor({ state: 'visible', timeout: 20_000 })
await trigger().click()
await item('area: apps').waitFor({ state: 'visible', timeout: 20_000 })

// Radix runs an enter animation (`animate-in fade-in-0 zoom-in-95`) and
// Playwright's visibility wait resolves while it is still running -- MEASURED at
// opacity 0.35 with one animation in flight, which photographed the rows bleeding
// through a half-transparent menu. Awaiting the animations makes the frame show
// what a user actually sees (opacity 1); the menu's background was never the
// problem.
await page.locator('[role="menu"]').evaluate(
  (el) => Promise.all(el.getAnimations().map((a) => a.finished)),
)

const offered = await page.getByRole('menuitem').count()
if (offered < 2) throw new Error(`expected the menu to offer labels, saw ${offered}`)
await page.screenshot({ path: join(OUT, '01-picker-open.png') })
console.log(`captured 01-picker-open.png (${total} rows, ${offered} labels offered)`)

// Drive the REAL controls.
// Both picks happen in ONE open pass, which is the multi-select behaviour the
// component's preventDefault buys; if the menu closed after the first, the
// second click would miss and the row count below would not reach 3.
await item('area: apps').click()
await item('bug').click()
await page.keyboard.press('Escape')
await page.locator('[role="menu"]').waitFor({ state: 'hidden', timeout: 20_000 })
const label = (await trigger().innerText()).trim()
for (const name of ['area: apps', 'bug']) {
  if (!label.includes(name)) {
    throw new Error(`trigger does not name the selection: saw "${label}", wanted "${name}"`)
  }
}

const narrowed = await rows().count()
if (narrowed !== 3) throw new Error(`expected 3 rows after OR-ing two labels, saw ${narrowed}`)

await page.screenshot({ path: join(OUT, '02-two-labels-or.png') })
console.log(`captured 02-two-labels-or.png (${narrowed} rows, trigger reads "${label}")`)

if (problems.length) {
  console.error(`page problems:\n  ${problems.join('\n  ')}`)
}
await browser.close()
await vite.close()
if (problems.length) process.exit(1)
