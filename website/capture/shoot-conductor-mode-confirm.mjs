/**
 * Screenshot harness for turning conductor mode on in a chat that has messages.
 *
 * Serves capture/conductor-mode-confirm.tsx with a programmatic Vite dev server
 * on loopback and photographs two scenes, in English and German:
 * the confirm dialog the mid-chat switch asks, and the notice a switch off a
 * locked conductor mode chat shows. These are component renders with seeded
 * store rows, not the live page.
 *
 * Usage (from website/): node capture/shoot-conductor-mode-confirm.mjs [outDir] [label]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/conductor-mode-confirm')
const LABEL = process.argv[3] || 'after'
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
const page = await browser.newPage({ viewport: { width: 720, height: 640 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
page.on('console', m => { if (m.type() === 'error') console.log('CONSOLE ERROR:', m.text()) })

for (const scene of ['confirm', 'blocked']) {
  for (const lang of ['en', 'de']) {
    await page.goto(`${base}/capture/conductor-mode-confirm.html?scene=${scene}&lang=${lang}`)
    await page.locator('[data-scene="page"]').waitFor({ timeout: 20000 })
    if (scene === 'confirm') await page.getByRole('dialog').waitFor({ timeout: 5000 })
    else await page.locator('[data-settled="notice"]').waitFor({ timeout: 5000 })
    // Let the dialog's entry animation finish.
    await page.waitForTimeout(400)
    const path = `${OUT}/${LABEL}-${scene}-${lang}.png`
    await page.screenshot({ path, animations: 'disabled' })
    console.log('wrote', path)
  }
}

await browser.close()
await server.close()
