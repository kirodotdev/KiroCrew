/**
 * Screenshot harness for the Explore panel. Serves capture/explore-sheet.tsx
 * with a loopback Vite dev server and photographs the REAL sheet with
 * fabricated milestones data, desktop and phone, both themes, with and
 * without the intro clip card.
 *
 * Usage (from website/): node capture/shoot-explore-sheet.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/explore-sheet')
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
for (const [name, viewport] of [['desktop', { width: 900, height: 760 }], ['phone', { width: 390, height: 760 }]]) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  for (const theme of ['dark', 'light']) {
    for (const video of ['1', '0']) {
      await page.goto(`${base}/capture/explore-sheet.html?theme=${theme}&video=${video}`)
      await page.getByTestId('milestones-sheet').waitFor({ timeout: 20000 })
      await page.waitForTimeout(600)
      const path = `${OUT}/${name}-${theme}-video${video}.png`
      await page.screenshot({ path })
      console.log('wrote', path)
    }
  }
  await page.close()
}
await browser.close()
await server.close()
