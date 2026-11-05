/**
 * Screenshot harness for a sub-agent's approval card in its chat (#28691).
 *
 * Serves the isolated capture entry (capture/subagent-approval-kept.tsx) with a
 * programmatic Vite dev server on loopback, then photographs the REAL composer
 * after a sub-agent's approval frame and a later user message, in both themes.
 * Every row is fabricated inside the entry, so no gateway / kiro-cli / token is
 * involved.
 *
 * Usage (from website/): node capture/shoot-subagent-approval-kept.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/28691')
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
const page = await browser.newPage({ viewport: { width: 960, height: 360 }, deviceScaleFactor: 2 })
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERROR:', m.text()) })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/subagent-approval-kept.html?theme=${theme}`)
  await page.locator('[data-capture-root] textarea, [data-capture-root] [contenteditable="true"]').first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(800)
  const path = `${OUT}/${PREFIX}-${theme}.png`
  await page.locator('[data-capture-root]').screenshot({ path })
  console.log('wrote', path)
}

await browser.close()
await server.close()
