/**
 * Screenshot harness for an inactive side-panel tab chip under keyboard focus.
 *
 * Serves the isolated capture entry (capture/tab-chip-focus-close.tsx) with a
 * programmatic Vite dev server on loopback, focuses the inactive chip with Tab
 * from the keyboard, and photographs the strip in both themes. No gateway is
 * involved.
 *
 * Usage (from website/): node capture/shoot-tab-chip-focus-close.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/chat-a11y')
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

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
const page = await browser.newPage({ viewport: { width: 520, height: 140 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/tab-chip-focus-close.html?theme=${theme}`)
  const chip = page.getByTestId('inactive-chip')
  await chip.waitFor({ timeout: 20000 })
  // Keyboard focus, not a click: tab from the start of the page until the
  // inactive chip holds it, so the frame shows what a keyboard user sees.
  for (let i = 0; i < 6; i += 1) {
    await page.keyboard.press('Tab')
    if (await chip.evaluate(el => el === document.activeElement)) break
  }
  await page.waitForTimeout(400)
  const path = `${OUT}/${PREFIX}-tab-chip-focus-${theme}.png`
  await page.locator('[data-capture-root]').screenshot({ path })
  console.log('wrote', path, 'focused=', await chip.evaluate(el => el === document.activeElement))
}

await browser.close()
await server.close()
