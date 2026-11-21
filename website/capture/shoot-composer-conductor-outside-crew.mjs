/**
 * Screenshot harness for a new conductor chat that another session opened
 * outside any crew, beside a conductor mode root for comparison.
 *
 * Serves capture/composer-conductor-outside-crew.tsx with a programmatic Vite
 * dev server on loopback. These are component renders of the real ChatPage
 * with a seeded store row and empty api reads, not the live page.
 *
 * Usage (from website/): node capture/shoot-composer-conductor-outside-crew.mjs [outDir] [label]
 * VITE_CACHE_DIR sets Vite's dependency cache (default: Vite's own).
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/composer-conductor-outside-crew')
const LABEL = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  ...(process.env.VITE_CACHE_DIR ? { cacheDir: process.env.VITE_CACHE_DIR } : {}),
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 900, height: 400 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const scene of ['outside', 'root']) {
  for (const theme of ['dark', 'light']) {
    await page.goto(`${base}/capture/composer-conductor-outside-crew.html?scene=${scene}&theme=${theme}`)
    const root = page.locator('[data-capture-root]')
    await root.waitFor({ timeout: 30000 })
    await page.waitForTimeout(800)
    const facts = {
      picker: await page.getByRole('button', { name: /^Agent: / }).count(),
      pressedChip: await page.locator('[data-testid="crew-mode-chip"][aria-pressed="true"]').count(),
      memoryChip: await page.getByTestId('memory-mode-chip').count(),
    }
    const path = `${OUT}/${LABEL}-${scene}-${theme}.png`
    await root.screenshot({ path, animations: 'disabled' })
    console.log(JSON.stringify({ path, ...facts }))
  }
}

await browser.close()
await server.close()
