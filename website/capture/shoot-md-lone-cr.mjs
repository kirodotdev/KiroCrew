/**
 * Screenshot harness for a fenced code block with lone carriage returns.
 *
 * Usage (from website/): node capture/shoot-md-lone-cr.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const OUT = process.argv[2] || resolve(here, '../../temp-screenshots/md-lone-cr')
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
const page = await browser.newPage({ viewport: { width: 760, height: 600 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
page.on('console', m => { if (m.type() === 'error') console.log('CONSOLE ERROR:', m.text().slice(0, 200)) })

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/md-lone-cr.html?theme=${theme}`)
  await page.waitForFunction(() => document.body.textContent?.includes('That is the whole tree'))
  await page.waitForTimeout(4000)
  const shot = join(OUT, `${PREFIX}-${theme}.png`)
  await page.locator('#root > div').screenshot({ path: shot })
  console.log('wrote', shot)
}

await browser.close()
await server.close()
