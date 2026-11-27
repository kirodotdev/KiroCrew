/**
 * Screenshot harness for the model list that kiro-cli's own sign-in holds back.
 *
 * Serves capture/kiro-cli-models-sign-in.tsx with a programmatic Vite dev server
 * on loopback and photographs the composer picker's sign-in row, the Run in
 * terminal confirm its button opens, and the Settings ▸ Chat banner. Component
 * renders with mocked data, not the live page.
 *
 * Usage (from website/): node capture/shoot-kiro-cli-models-sign-in.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/kiro-cli-models-sign-in')
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
const SHOTS = [
  { scenario: 'picker', viewport: { width: 420, height: 360 }, ready: '[data-testid="kiro-cli-models-signed-out"]' },
  { scenario: 'confirm', viewport: { width: 720, height: 420 }, ready: 'text=kiro-cli login' },
  { scenario: 'settings', viewport: { width: 1100, height: 420 }, ready: '[data-testid="kiro-cli-sign-in-terminal"]' },
  { scenario: 'picker', name: 'picker-copy', canRun: false, viewport: { width: 420, height: 360 }, ready: '[data-testid="kiro-cli-sign-in-copy"]' },
  { scenario: 'settings', name: 'settings-copy', canRun: false, viewport: { width: 1100, height: 420 }, ready: '[data-testid="kiro-cli-sign-in-copy"]' },
]
for (const shot of SHOTS) {
  const page = await browser.newPage({ viewport: shot.viewport, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  for (const theme of ['light', 'dark']) {
    await page.goto(`${base}/capture/kiro-cli-models-sign-in.html?scenario=${shot.scenario}&theme=${theme}&lang=en&canrun=${shot.canRun === false ? '0' : '1'}`)
    await page.locator(shot.ready).first().waitFor({ timeout: 20000 })
    const path = `${OUT}/${shot.name || shot.scenario}-${theme}.png`
    await page.screenshot({ path, animations: 'disabled' })
    console.log('wrote', path)
  }
  await page.close()
}

await browser.close()
await server.close()
