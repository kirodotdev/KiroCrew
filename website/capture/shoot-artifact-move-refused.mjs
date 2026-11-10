/**
 * Screenshot harness for a refused artifact folder move (#34554).
 *
 * Serves capture/artifact-move-refused.tsx with a programmatic Vite dev server on
 * loopback and photographs what the user sees after the move is refused, in
 * English and German, light theme. Run it at the base commit too: there the
 * frame stays empty, which is the silent rollback this fixes.
 *
 * Usage (from website/): node capture/shoot-artifact-move-refused.mjs [outDir] [label]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/artifact-move-refused')
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
const page = await browser.newPage({ viewport: { width: 720, height: 260 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const theme of ['light']) {
  for (const lang of ['en', 'de']) {
    await page.goto(`${base}/capture/artifact-move-refused.html?theme=${theme}&lang=${lang}`)
    const scene = page.locator('[data-scene="page"]')
    await scene.waitFor({ timeout: 20000 })
    // The refusal settles after the mocked request rejects; the base commit
    // never shows a notice, so a bounded wait for it is expected to time out there.
    await page.locator('[data-settled="notice"]').waitFor({ timeout: 3000 }).catch(() => {})
    const path = `${OUT}/${LABEL}-${lang}-${theme}.png`
    // The banner is position: fixed, so the frame is the viewport, not the scene box.
    await page.screenshot({ path, animations: 'disabled' })
    console.log('wrote', path)
  }
}

await browser.close()
await server.close()
