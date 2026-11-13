/**
 * Screenshot harness for the gateway status-row notices.
 *
 * Serves the isolated capture entry (capture/gateway-notice.tsx) with a
 * programmatic Vite dev server on loopback, then photographs the REAL transcript
 * renderers drawing each notice row in English and German, in both themes. Every
 * row is fabricated inside the entry, so no gateway / kiro-cli / token is involved.
 *
 * Usage (from website/): node capture/shoot-gateway-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/gateway-notice')
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
const page = await browser.newPage({ viewport: { width: 720, height: 6000 }, deviceScaleFactor: 2 })
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERROR:', m.text()) })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const theme of ['dark', 'light']) {
  for (const lang of ['en', 'de']) {
    await page.goto(`${base}/capture/gateway-notice.html?theme=${theme}&lang=${lang}`)
    await page.locator('[data-scene="rows"]').waitFor({ timeout: 20000 })
    await page.waitForTimeout(500)
    for (const scene of ['rows', 'more', 'recovery', 'turn']) {
      const path = `${OUT}/${scene}-${lang}-${theme}.png`
      await page.locator(`[data-scene="${scene}"]`).screenshot({ path })
      console.log('wrote', path)
    }
  }
}

await browser.close()
await server.close()
