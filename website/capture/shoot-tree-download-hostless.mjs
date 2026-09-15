/**
 * Screenshot of the HOSTLESS file-tree row menu (capture/tree-download-hostless.html).
 *
 * Renders `PierreWorkspaceTree` with no `onAddToContext` (the Members-DM mount)
 * and captures the one frame the SPA harness cannot reach: a FILE row whose
 * context menu holds `Download` as its ONLY item. Self-checking -- it asserts
 * exactly one menuitem, that it reads `Download`, and that no `Add to chat` row
 * is present, before screenshotting. A screenshot of the wrong state is worse
 * evidence than none.
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6819 --strictPort   # in another shell
 *   node capture/shoot-tree-download-hostless.mjs http://127.0.0.1:6819 ../temp-screenshots/tree-download-menu
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6819'
const OUT = process.argv[3] || '../temp-screenshots/tree-download-menu'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 520, height: 620 }, deviceScaleFactor: 2 })
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERROR', m.text()) })

await page.goto(`${BASE}/capture/tree-download-hostless.html?theme=dark`, { waitUntil: 'domcontentloaded' })

// The tree renders inside a `<file-tree-container>` shadow root; wait for the
// seeded file row's button (labeled by exact aria-label, not the truncated text).
await page.waitForFunction(
  () => !!document.querySelector('file-tree-container')?.shadowRoot?.querySelector('button[aria-label="README.md"]'),
  { timeout: 20000 },
)
await page.waitForTimeout(600)

// Screen-space center of the row button whose aria-label equals `label`.
const rowCenter = (label) => page.evaluate((lbl) => {
  const root = document.querySelector('file-tree-container')?.shadowRoot
  if (!root) return null
  const el = root.querySelector(`button[aria-label="${lbl}"]`)
  if (!el) return { notFound: true }
  const r = el.getBoundingClientRect()
  return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) }
}, label)

const c = await rowCenter('README.md')
if (!c || c.notFound) throw new Error(`README.md row not found: ${JSON.stringify(c)}`)
await page.mouse.move(c.x, c.y)
await page.waitForTimeout(150)
await page.mouse.click(c.x, c.y, { button: 'right' })

const menu = page.locator('[role="menu"]').first()
await menu.waitFor({ state: 'visible', timeout: 8000 })
await page.waitForTimeout(300)

const rows = await page.locator('[role="menu"] [role="menuitem"]').allInnerTexts()
console.log('DIAG hostless menu rows', JSON.stringify(rows))
if (rows.length !== 1) throw new Error(`expected exactly one menu item, got ${JSON.stringify(rows)}`)
if (!/Download/.test(rows[0])) throw new Error(`expected the sole item to be Download, got ${JSON.stringify(rows)}`)
if (rows.some(t => /Add to chat/.test(t))) throw new Error(`Add to chat must be absent in the hostless menu, got ${JSON.stringify(rows)}`)

const file = join(OUT, '24-tree-download-only-nohost.png')
await menu.screenshot({ path: file })
console.log(`wrote ${file}  rows=${JSON.stringify(rows)}`)

await browser.close()
console.log(`done → ${OUT}`)
