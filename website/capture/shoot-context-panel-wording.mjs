/**
 * Screenshot harness for the Context panel wording (#15520).
 *
 * Serves the isolated capture entry (capture/context-panel-wording.tsx) with a
 * programmatic Vite dev server on loopback, then photographs each scene of the
 * REAL ContextBreakdownPanel in both themes. Every trace is fabricated inside the
 * entry, so no gateway / kiro-cli / token is involved.
 *
 * Usage (from website/): node capture/shoot-context-panel-wording.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/context-panel-wording')
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
const page = await browser.newPage({ viewport: { width: 560, height: 1400 }, deviceScaleFactor: 2 })
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERROR:', m.text()) })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/context-panel-wording.html?theme=${theme}`)
  await page.locator('[data-scene="single"]').waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  const scenes = await page.locator('[data-scene]').evaluateAll(els => els.map(e => e.getAttribute('data-scene')))
  for (const id of scenes) {
    const path = `${OUT}/${id}-${theme}.png`
    await page.locator(`[data-scene="${id}"]`).screenshot({ path })
    console.log('wrote', path)
  }
}

await browser.close()
await server.close()
