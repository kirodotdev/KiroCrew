/**
 * Screenshot harness for the Settings -> Voice error notices on a phone (#34540).
 *
 * Serves capture/voice-notice-phone-wrap.tsx with a programmatic Vite dev server
 * on loopback and photographs the four notices at 375px and 1024px, in English
 * and German. Run it at the base commit too, with the same entry: there the
 * `below-on-phone` value is unknown, so each notice renders its old beside shape.
 *
 * Usage (from website/): node capture/shoot-voice-notice-phone-wrap.mjs [outDir] [label]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/voice-notice-phone-wrap')
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
page_loop: for (const width of [375, 1024]) {
  const page = await browser.newPage({ viewport: { width, height: 900 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  for (const lang of ['en', 'de']) {
    await page.goto(`${base}/capture/voice-notice-phone-wrap.html?lang=${lang}${LABEL === 'before' ? '&before=1' : ''}`)
    const scene = page.locator('[data-scene="voice"]')
    try {
      await scene.waitFor({ timeout: 20000 })
    } catch {
      console.log('scene did not render at', width, lang)
      await page.close()
      continue page_loop
    }
    const path = `${OUT}/${LABEL}-${width}-${lang}.png`
    await scene.screenshot({ path, animations: 'disabled' })
    console.log('wrote', path)
  }
  await page.close()
}

await browser.close()
await server.close()
