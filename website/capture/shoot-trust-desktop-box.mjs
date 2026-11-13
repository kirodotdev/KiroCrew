/**
 * Screenshot harness for the trust prompt's desktop box (capture/trust-desktop-box.tsx).
 *
 * Serves the isolated entry with a programmatic Vite dev server on loopback and
 * shoots the dialog for both scenes in both themes, plus one translated catalog.
 * Nothing is fetched from a gateway.
 *
 * Usage (from website/): node capture/shoot-trust-desktop-box.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/trust-desktop-box')
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

const shots = [
  ['spanning', 'dark', 'en'], ['spanning', 'light', 'en'],
  ['shortcut', 'dark', 'en'], ['spanning', 'dark', 'de'],
]
for (const [scene, theme, lang] of shots) {
  const page = await browser.newPage({ viewport: { width: 900, height: 1200 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  await page.goto(`${base}/capture/trust-desktop-box.html?scene=${scene}&theme=${theme}&lang=${lang}`)
  await page.getByTestId('trust-desktop-box').waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  const path = `${OUT}/${scene}-${theme}-${lang}.png`
  await page.getByRole('dialog').screenshot({ path })
  console.log('wrote', path)
  await page.close()
}

await browser.close()
await server.close()
