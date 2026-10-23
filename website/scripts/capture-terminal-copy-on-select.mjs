/**
 * Screenshots of terminal copy-on-select (Part of #20929).
 *
 * Drives the isolated capture entry (website/capture/terminal-copy-on-select.html),
 * which mounts the REAL CliPanel with only its PTY socket faked. Each scene
 * drags across a line of terminal output with a real mouse and shoots the
 * selection toolbar that opens:
 *   on   the preference on: the toolbar opens already showing ✓ Copied, and the
 *        clipboard holds the selected text (asserted before the frame is kept).
 *   off  the preference off: the toolbar opens on Copy, the clipboard is untouched.
 *
 * Usage (from website/; starts its own Vite dev server and stops it at the end):
 *   node scripts/capture-terminal-copy-on-select.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-copy-on-select'
mkdirSync(OUT, { recursive: true })

const vite = await createServer({ server: { host: '127.0.0.1', port: 0 }, logLevel: 'warn' })
await vite.listen()
const BASE = vite.resolvedUrls.local[0].replace(/\/$/, '')

const SCENES = [
  { name: 'terminal-on', copy: 'on', expect: 'Copied' },
  { name: 'terminal-off', copy: 'off', expect: 'Copy' },
]

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  // Strip mise's LD_LIBRARY_PATH (see shoot-terminal-cursor-style.mjs), but keep
  // one set for an explicit PLAYWRIGHT_CHROMIUM build that ships its own libs.
  env: { ...process.env, LD_LIBRARY_PATH: process.env.PLAYWRIGHT_CHROMIUM ? (process.env.LD_LIBRARY_PATH ?? '') : '' },
})
const context = await browser.newContext({ viewport: { width: 760, height: 280 }, deviceScaleFactor: 2 })
await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE })
const page = await context.newPage()

let failed = false
for (const s of SCENES) {
  await page.goto(`${BASE}/capture/terminal-copy-on-select.html?theme=dark&copy=${s.copy}`)
  await page.evaluate(() => navigator.clipboard.writeText('(clipboard before)'))
  const row = page.locator('.xterm-rows > div').nth(2)
  await row.waitFor()
  await page.waitForFunction(() => document.querySelector('.xterm-rows')?.textContent?.includes('api-7d9f8c6b5'))
  // Drag across the pod name on the third row, like a user grabbing an id.
  // The drag ends inside the last cell of the name, measured from the rendered text.
  const span = await row.evaluate(el => {
    const node = document.createTreeWalker(el, NodeFilter.SHOW_TEXT).nextNode()
    const r = document.createRange()
    r.setStart(node, 0)
    r.setEnd(node, 'api-7d9f8c6b5-x2kqp'.length)
    const b = r.getBoundingClientRect()
    return { x: b.left, right: b.right, y: b.top + b.height / 2 }
  })
  await page.mouse.move(span.x + 1, span.y)
  await page.mouse.down()
  await page.mouse.move((span.x + span.right) / 2, span.y, { steps: 6 })
  await page.mouse.move(span.right - 2, span.y, { steps: 6 })
  await page.mouse.up()
  const button = page.getByRole('button', { name: new RegExp(`^${s.expect}$`) })
  await button.waitFor({ timeout: 5000 })
  await page.waitForTimeout(200)
  const clip = await page.evaluate(() => navigator.clipboard.readText())
  const wantClip = s.copy === 'on' ? clip === 'api-7d9f8c6b5-x2kqp' : clip === '(clipboard before)'
  if (!wantClip) { console.error(`${s.name}: unexpected clipboard ${JSON.stringify(clip)}`); failed = true; continue }
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${s.name}.png` })
  console.log(`shot ${s.name} (clipboard: ${JSON.stringify(clip)})`)
}

await browser.close()
await vite.close()
if (failed) process.exit(1)
console.log('frames written to', OUT)
