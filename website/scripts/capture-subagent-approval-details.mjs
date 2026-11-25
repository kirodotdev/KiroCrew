/**
 * Screenshot harness: the details panel of a KAS sub-agent dispatch's approval
 * card (live, no input) and of a bare historical tool row, in light and dark.
 * Fails when the live card says its details were not saved.
 *
 * Gateway-free: an in-process vite dev server serves the build-excluded capture
 * entry (capture/subagent-approval-details.html).
 *
 * Usage: node scripts/capture-subagent-approval-details.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || 'temp-screenshots/subagent-approval-details'
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
    const context = await browser.newContext({ deviceScaleFactor: 2, viewport: { width: 820, height: 600 } })
    const page = await context.newPage()
    page.on('pageerror', e => { console.log(`FAIL pageerror: ${e.message}`); failed = true })
    await page.goto(`${BASE}/capture/subagent-approval-details.html?theme=${theme}`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="approval-subagent-task"]', { timeout: 30_000 })
    await page.evaluate(() => document.fonts.ready)
    await page.waitForTimeout(500)
    const live = await page.locator('[data-testid="live-card"]').innerText()
    const old = await page.locator('[data-testid="historical-row"]').innerText()
    console.log(`${theme} live: ${JSON.stringify(live)}`)
    console.log(`${theme} historical: ${JSON.stringify(old)}`)
    if (/weren't saved|historical/i.test(live)) { console.log('FAIL the live card reads as historical'); failed = true }
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })
    console.log(`wrote ${OUT}/${theme}.png`)
    await context.close()
  }
} finally {
  await browser.close()
  await vite.close()
}
process.exit(failed ? 1 : 0)
