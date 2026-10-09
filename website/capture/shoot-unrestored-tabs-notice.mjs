/**
 * Screenshot harness for the "N tabs were not restored" chat-pane notice.
 *
 * Serves the isolated capture entry (capture/unrestored-tabs-notice.tsx) with a
 * programmatic Vite dev server on loopback, then photographs the REAL
 * UnrestoredTabsNotice in both themes, at the plural count the reported incident
 * produced and at one tab. The arrival read is stubbed inside the entry, so no
 * gateway / kiro-cli / token is involved.
 *
 * Usage (from website/): node capture/shoot-unrestored-tabs-notice.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT =
  process.argv[2] ||
  resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/18252-unrestored-tabs')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5203, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base =
  server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') ||
  `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 980, height: 400 }, deviceScaleFactor: 2 })
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERROR:', m.text()) })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

// 16 is the count the reported incident produced; 1 proves the singular key is a
// real sentence rather than "1 tabs"; `fail` is the branch that says the CHECK
// failed, where going silent would read as "nothing was dropped".
const SCENES = [
  ['sixteen', '&count=16', 'unrestored-tabs-notice'],
  ['one', '&count=1', 'unrestored-tabs-notice'],
  ['read-failed', '&fail=1', 'unrestored-tabs-read-failed'],
]

for (const [name, query, testid] of SCENES) {
  for (const theme of ['dark', 'light']) {
    await page.goto(`${base}/capture/unrestored-tabs-notice.html?theme=${theme}${query}`)
    await page.locator(`[data-testid="${testid}"]`).waitFor({ timeout: 20000 })
    await page.waitForTimeout(300)
    const path = `${OUT}/${name}-${theme}.png`
    await page.locator('[data-capture-root]').screenshot({ path })
    console.log('wrote', path)
  }
}

await browser.close()
await server.close()
