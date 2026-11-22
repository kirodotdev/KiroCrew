/** Real-browser evidence for the App SDK `BrowserView` action list.
 *
 * Scenes: the list open, the list folded, and the view before its first picture.
 * Dark and light. Mounts through capture/app-browser-view-actions.html; the
 * gateway is not needed. Serves the dev build itself (vite in-process, loopback).
 *
 * Usage (in website/):
 *   node scripts/capture-app-browser-view-actions.mjs ../temp-screenshots/app-browser-view-actions
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/app-browser-view-actions')
mkdirSync(OUT, { recursive: true })

const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5199, strictPort: false }, logLevel: 'warn' })
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
try {
  for (const theme of ['dark', 'light']) {
    for (const scene of ['open', 'folded']) {
      const page = await browser.newPage({ viewport: { width: 820, height: 760 }, deviceScaleFactor: 2 })
      await page.goto(`${BASE}/capture/app-browser-view-actions.html?scene=${scene}&theme=${theme}`)
      await page.waitForSelector('body[data-ready="1"]', { timeout: 30_000 })
      const toggle = page.getByTestId('app-browser-view-actions-toggle')
      const expanded = await toggle.getAttribute('aria-expanded')
      const items = await page.getByTestId('app-browser-view-actions-list').locator('li').count().catch(() => 0)
      const want = scene === 'open' ? ['true', 5] : ['false', 0]
      if (expanded !== want[0] || items !== want[1]) { failures++; console.error(`FAIL ${theme}/${scene}: expanded=${expanded} items=${items}`) }
      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/actions-${scene}-${theme}.png` })
      await page.close()
    }
  }
} finally {
  await browser.close()
  await server.close()
}
console.log(failures ? `${failures} check(s) failed` : `ok: wrote ${OUT}`)
process.exit(failures ? 1 : 0)
