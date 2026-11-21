/**
 * Screenshot harness for the App SDK refusals (capture/app-sdk-refusals.tsx).
 *
 * Serves the isolated entry with a programmatic Vite dev server on loopback and
 * shoots the page and each component's refusal frame: dark and light at 760px,
 * dark at 320px.
 * The entry declares no API path, so no request is sent; no gateway or token is
 * involved.
 *
 * Usage (from website/): node capture/shoot-app-sdk-refusals.mjs [outDir] [lang]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/app-sdk-refusals')
const LANG = process.argv[3] || 'en'
mkdirSync(OUT, { recursive: true })

const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5198, strictPort: false }, logLevel: 'warn' })
await server.listen()
const origin = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })

const FRAMES = ['pr', 'picker', 'issues', 'changes']
for (const [theme, width] of [['dark', 760], ['light', 760], ['dark', 320]]) {
  const page = await browser.newPage({ viewport: { width, height: 1200 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  await page.goto(`${origin}/capture/app-sdk-refusals.html?theme=${theme}&lang=${LANG}`)
  for (const id of FRAMES) await page.getByTestId(`frame-${id}`).getByRole('alert').waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  await page.getByTestId('frames').screenshot({ path: `${OUT}/all-${LANG}-${theme}-${width}.png` })
  for (const id of FRAMES) {
    const file = `${OUT}/${id}-${LANG}-${theme}-${width}.png`
    await page.getByTestId(`frame-${id}`).screenshot({ path: file })
    console.log('wrote', file)
  }
  await page.close()
}

await browser.close()
await server.close()
