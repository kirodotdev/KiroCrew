/**
 * Screenshot harness for theme-overlay-remote-crew: main's mount vs this
 * change on a remote Crew pane, plus the local dashboard after the change.
 * Prints whether the mascot is in the DOM for each arm.
 *
 * Usage (from website/): node capture/shoot-theme-overlay-remote-crew.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const OUT = process.argv[2] || resolve(here, '../../temp-screenshots/theme-overlay-remote-crew')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '')
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })

for (const [name, query] of [['before-remote', 'arm=before&crew=remote'], ['after-remote', 'arm=after&crew=remote'], ['after-local', 'arm=after&crew=local']]) {
  const page = await browser.newPage({ viewport: { width: 1100, height: 520 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  await page.goto(`${base}/capture/theme-overlay-remote-crew.html?${query}`)
  await page.getByText('the end of this line must stay readable').waitFor()
  const mascot = await page.locator('[data-capture-mascot]').count()
  console.log(name, 'mascot in DOM:', mascot > 0)
  const shot = join(OUT, `${name}.png`)
  await page.screenshot({ path: shot })
  console.log('wrote', shot)
  await page.close()
}
await browser.close()
await server.close()
