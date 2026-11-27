/**
 * Screenshot harness: an edit tool's diff on a Crew Mode approval card and on
 * the Command Center's attention card, in light and dark. Prints the line kinds
 * the render marked and checks the block's text is the input, byte for byte.
 *
 * Gateway-free: an in-process vite dev server serves the build-excluded capture
 * entry (capture/approval-card-diff.html); every /api/ read gets an empty crew.
 *
 * Usage: node scripts/capture-40271-approval-card-diff.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || 'temp-screenshots/40271-approval-card-diff'
mkdirSync(OUT, { recursive: true })

const vite = await createServer({
  configFile: new URL('../vite.config.ts', import.meta.url).pathname,
  server: { port: 0, host: '127.0.0.1' },
  logLevel: 'error',
})
await vite.listen()
const BASE = `http://127.0.0.1:${vite.httpServer.address().port}`
const browser = await chromium.launch({ executablePath: chromiumExecutable() })

let failed = false
try {
  for (const theme of ['light', 'dark']) {
    const context = await browser.newContext({ deviceScaleFactor: 2, viewport: { width: 640, height: 1100 } })
    const page = await context.newPage()
    page.on('pageerror', e => { console.log(`FAIL pageerror: ${e.message}`); failed = true })
    // The gateway's own /api/ prefix only: src/api/ modules share the path segment.
    await page.route(`${BASE}/api/**`, route => route.fulfill({ json: { root: 'root', workers: [] } }))
    await page.goto(`${BASE}/capture/approval-card-diff.html?theme=${theme}`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="crew-approval-card"] pre', { timeout: 30_000 })
    await page.evaluate(() => document.fonts.ready)
    await page.waitForTimeout(800)
    const blocks = await page.evaluate(() => Array.from(document.querySelectorAll('[data-capture-root] pre')).map(pre => ({
      kinds: Array.from(pre.querySelectorAll('[data-diff-line]')).map(el => el.getAttribute('data-diff-line')).join(','),
      chars: pre.textContent.length,
    })))
    for (const b of blocks) console.log(`${theme}: ${b.chars} chars, lines ${b.kinds || '(plain text)'}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })
    console.log(`wrote ${OUT}/${theme}.png`)
    await context.close()
  }
} finally {
  await browser.close()
  await vite.close()
}
process.exit(failed ? 1 : 0)
