/**
 * Screenshot harness for a file_send image card that opens the image viewer.
 *
 * Serves capture/file-card-image-viewer.html from a programmatic Vite dev
 * server on loopback; /api/outbox/* and /api/file-raw answer with fixture PNGs
 * from .github/screenshots, so nothing reaches a backend.
 *
 * Frames: the card at rest (dark + light), the preview hovered and
 * keyboard-focused, the viewer opened from the card with Enter, and a
 * preview whose outbox request fails (dark + light).
 *
 * Usage (from website/): node capture/shoot-file-card-image-viewer.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const OUT = process.argv[2] || resolve(here, '../../temp-screenshots/file-card-image-viewer')
mkdirSync(OUT, { recursive: true })

const shots = resolve(here, '../../.github/screenshots/15073')
const CARD_PNG = readFileSync(join(shots, 'pipeline-board-full-dark.png'))
const MD_PNG = readFileSync(join(shots, 'pipeline-board-full-light.png'))

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})

async function openPage(theme) {
  const page = await browser.newPage({ viewport: { width: 800, height: 900 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  await page.route('**/api/outbox/**', route => route.fulfill({ status: 200, contentType: 'image/png', body: CARD_PNG }))
  await page.route('**/api/file-raw*', route => route.fulfill({ status: 200, contentType: 'image/png', body: MD_PNG }))
  await page.goto(`${base}/capture/file-card-image-viewer.html?theme=${theme}`)
  const card = page.locator('[data-testid="file-card-image-open"]')
  await card.waitFor()
  await page.waitForFunction(() => Array.from(document.images).every(i => i.complete && i.naturalWidth > 0))
  return { page, card }
}

const root = '[data-capture-root]'

for (const theme of ['dark', 'light']) {
  const { page } = await openPage(theme)
  await page.locator(root).screenshot({ path: join(OUT, `01-card-at-rest-${theme}.png`) })
  await page.close()
}

{
  const { page, card } = await openPage('dark')
  await card.hover()
  await page.waitForTimeout(250) // the hover fade is a 150ms CSS transition
  await page.locator(root).screenshot({ path: join(OUT, '02-card-hover-dark.png') })
  await page.mouse.move(790, 10)
  // Keyboard focus: Tab from the document start until the preview button holds it.
  for (let i = 0; i < 20; i++) {
    await page.keyboard.press('Tab')
    if (await card.evaluate(el => el === document.activeElement)) break
  }
  await page.waitForTimeout(250)
  await page.locator(root).screenshot({ path: join(OUT, '03-card-keyboard-focus-dark.png') })
  await page.keyboard.press('Enter')
  await page.locator('[data-testid="lightbox-toolbar"]').waitFor()
  await page.waitForTimeout(300)
  const state = await page.evaluate(() => ({
    shown: document.querySelector('.fixed.inset-0 img')?.getAttribute('src'),
    position: Array.from(document.querySelectorAll('[aria-live="polite"]')).map(n => n.textContent).filter(Boolean),
  }))
  console.log('viewer:', JSON.stringify(state))
  await page.screenshot({ path: join(OUT, '04-viewer-open-from-card-dark.png') })
  await page.keyboard.press('ArrowLeft')
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, '05-viewer-paged-to-markdown-image-dark.png') })
  await page.close()
}

// Broken preview: the outbox answers 404, so the card shows its inline error.
for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 800, height: 900 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  await page.route('**/api/outbox/**', route => route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"not found"}' }))
  await page.route('**/api/file-raw*', route => route.fulfill({ status: 200, contentType: 'image/png', body: MD_PNG }))
  await page.goto(`${base}/capture/file-card-image-viewer.html?theme=${theme}`)
  await page.locator('[data-testid="file-card-image-error"]').waitFor()
  await page.waitForFunction(() => Array.from(document.images).every(i => i.complete))
  await page.locator(root).screenshot({ path: join(OUT, `06-card-broken-preview-${theme}.png`) })
  await page.close()
}

await browser.close()
await server.close()
console.log(`wrote ${OUT}`)
