/** Real-browser evidence for Ctrl+F over an edit-mode diff in the side panel (#30319).
 *
 * Starts Vite in-process, drives the ISOLATED capture entry
 * (website/capture/panel-diff-ctrlf.html, the REAL `MarkdownPanel` over a
 * fixture fetch boundary), clicks a deleted line of the diff and presses
 * Ctrl+F, per theme. It records whether the chat-find stand-in answered the
 * chord: on main it does, with this fix it does not.
 *
 * Usage (from website/):
 *   node scripts/capture-30319-panel-diff-ctrlf.mjs [outDir]
 * Set PW_LD_LIBRARY_PATH when the host lacks the browser's shared libraries.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/30319'
mkdirSync(OUT, { recursive: true })
const PORT = 6883

const server = await createServer({ server: { host: '127.0.0.1', port: PORT, strictPort: true } })
await server.listen()
const { LD_LIBRARY_PATH: _mise, ...env } = process.env
if (process.env.PW_LD_LIBRARY_PATH) env.LD_LIBRARY_PATH = process.env.PW_LD_LIBRARY_PATH
const browser = await chromium.launch({ env })

try {
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage({ viewport: { width: 1100, height: 480 } })
    page.on('pageerror', e => console.error(`[${theme}] pageerror:`, e.message))
    await page.goto(`http://127.0.0.1:${PORT}/capture/panel-diff-ctrlf.html?theme=${theme}`, { waitUntil: 'networkidle', timeout: 180000 })
    const deleted = page.locator('[data-line-type="change-deletion"]').first()
    await deleted.waitFor({ timeout: 60000 })
    await page.waitForTimeout(500)
    const box = await deleted.boundingBox()
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
    await page.keyboard.press('Control+f')
    await page.waitForTimeout(400)
    const chatFind = await page.locator('[data-chat-find]').count()
    console.log(`[${theme}] chat find opened: ${chatFind > 0}`)
    await page.screenshot({ path: `${OUT}/${theme}-ctrlf.png` })
    await page.close()
  }
} finally {
  await browser.close()
  await server.close()
}
