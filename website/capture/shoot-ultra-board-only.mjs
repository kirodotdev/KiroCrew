/**
 * Screenshot harness for "Ultra Mode shows only the Board".
 *
 * Serves capture/ultra-board-only.tsx with a programmatic Vite dev server on
 * loopback and photographs the REAL SidePanel's tab strip for an Ultra Mode root
 * and for a normal chat, both with a Dashboard tab stored. No gateway is involved.
 *
 * Usage (from website/): node capture/shoot-ultra-board-only.mjs [outDir]
 * SHOT_PREFIX names the files (e.g. `before` on main, `after` on the branch).
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/ultra-board-only')
mkdirSync(OUT, { recursive: true })
const PREFIX = process.env.SHOT_PREFIX || 'shot'

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
const page = await browser.newPage({ viewport: { width: 880, height: 520 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const slot of ['crew-root', 'plain']) {
  await page.goto(`${base}/capture/ultra-board-only.html?slot=${slot}`)
  await page.locator('[role="tab"]').first().waitFor({ timeout: 30000 })
  await page.waitForTimeout(800)
  const frame = page.locator('[data-testid="capture-frame"]')
  const box = await frame.boundingBox()
  await page.screenshot({ path: `${OUT}/${PREFIX}-${slot}.png`, clip: { x: box.x, y: box.y, width: Math.min(box.width, 330), height: Math.min(box.height, 60) } })
  console.log('wrote', `${OUT}/${PREFIX}-${slot}.png`)
  // The + menu, open: an Ultra Mode chat offers no Dashboard, a normal chat does.
  await page.getByRole('button', { name: 'Open side panel tab' }).click()
  await page.getByRole('menu').waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-${slot}-menu.png`, clip: { x: box.x, y: box.y, width: Math.min(box.width, 420), height: Math.min(box.height, 400) } })
  console.log('wrote', `${OUT}/${PREFIX}-${slot}-menu.png`)
}

await browser.close()
await server.close()
