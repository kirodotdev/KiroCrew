/**
 * Evidence for one click on a linked markdown image doing one thing.
 *
 * Serves capture/linked-image-click.html from a loopback Vite dev server,
 * answers /api/file-raw with a PNG, clicks the image once and records what
 * happened: whether the image viewer opened in this tab and how many new tabs
 * the click opened. Then it shoots the page.
 *
 * Usage (from website/): node capture/shoot-linked-image-click.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const outDir = process.argv[2] || resolve(here, '../../temp-screenshots/linked-image-click')
const prefix = process.argv[3] || 'after'
mkdirSync(outDir, { recursive: true })
const diagram = readFileSync(resolve(here, '../public/icon-512.png'))

const vite = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5203, strictPort: false }, logLevel: 'warn' })
await vite.listen()
const origin = vite.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${vite.config.server.port}`

const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })
const context = await browser.newContext({ viewport: { width: 900, height: 640 }, deviceScaleFactor: 2 })
await context.route('**/api/file-raw*', route => route.fulfill({ status: 200, contentType: 'image/png', body: diagram }))
const tab = await context.newPage()
await tab.goto(`${origin}/capture/linked-image-click.html`)
await tab.waitForFunction(() => [...document.querySelectorAll('img')].some(i => i.complete && i.naturalWidth > 0))
await tab.waitForTimeout(500)
await tab.screenshot({ path: join(outDir, `${prefix}-1-page.png`) })

await tab.locator('img[data-lightbox-image]').click()
await tab.waitForTimeout(1500)
const viewerOpen = await tab.locator('[data-testid="lightbox-toolbar"]').count() > 0
const newTabs = context.pages().length - 1
console.log(JSON.stringify({ prefix, viewerOpen, newTabs, tabs: context.pages().map(p => p.url()) }))
// The tab count is the bug, and a still cannot show a second tab, so the
// shot carries it as a caption.
await tab.evaluate(text => {
  const note = document.createElement('div')
  note.textContent = text
  note.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:2147483647;padding:8px 12px;border-radius:8px;background:#111;color:#fff;font:600 15px system-ui'
  document.body.appendChild(note)
}, `${prefix}: one click -> viewer ${viewerOpen ? 'open' : 'closed'}, new tabs opened: ${newTabs}`)
await tab.screenshot({ path: join(outDir, `${prefix}-2-after-click.png`) })

await browser.close()
await vite.close()
