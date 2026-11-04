/**
 * Screenshot harness for the warm brief option buttons (capture/warm-brief-options.tsx).
 *
 * Serves the capture entry from a loopback Vite dev server and photographs
 * before / after scenes in both themes. No gateway is involved.
 *
 * Usage (from website/): node capture/shoot-warm-brief-options.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/warm-brief-options')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })
const page = await browser.newPage({ viewport: { width: 720, height: 560 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

const shots = [['before', 'off'], ['after', 'on']]
for (const theme of ['dark', 'light']) {
  for (const [name, fix] of shots) {
    await page.goto(`${base}/capture/warm-brief-options.html?fix=${fix}&theme=${theme}`)
    await page.locator('[data-scene]').waitFor({ timeout: 20000 })
    await page.waitForTimeout(400)
    const path = `${OUT}/${name}-${theme}.png`
    await page.screenshot({ path })
    const pills = await page.locator('span.followup-chip').allTextContents()
    console.log('wrote', path, JSON.stringify(pills))
  }
}

await browser.close()
await server.close()
