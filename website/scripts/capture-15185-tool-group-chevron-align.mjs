/**
 * Screenshot harness: a settled tool-call group stacked above a "Sub-agents
 * finished" recovery card, in light and dark. Measures each header chevron's
 * left x and width from the rendered DOM and prints them, so the frame and the
 * numbers come from the same render.
 *
 * Gateway-free: an in-process vite dev server serves the build-excluded capture
 * entry (capture/tool-group-chevron-align.html).
 *
 * Usage: node scripts/capture-15185-tool-group-chevron-align.mjs [outDir] [--expect aligned]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const args = process.argv.slice(2)
const expectAligned = args.includes('--expect') && args[args.indexOf('--expect') + 1] === 'aligned'
const OUT = args.find(a => !a.startsWith('--') && a !== 'aligned') || 'temp-screenshots/15185-tool-group-chevron-align'
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
    const context = await browser.newContext({ deviceScaleFactor: 2, viewport: { width: 640, height: 260 } })
    const page = await context.newPage()
    page.on('pageerror', e => { console.log(`FAIL pageerror: ${e.message}`); failed = true })
    await page.goto(`${BASE}/capture/tool-group-chevron-align.html?theme=${theme}`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-testid="recovery-card-toggle"] svg.lucide-chevron-right', { timeout: 30_000 })
    await page.waitForFunction(
      () => (document.querySelector('[data-testid="recovery-card-toggle"]')?.textContent ?? '').includes('Sub-agents finished'),
      null, { timeout: 15_000 },
    )
    await page.evaluate(() => document.fonts.ready)
    await page.waitForTimeout(800)
    const m = await page.evaluate(() => {
      const box = el => { const r = el.getBoundingClientRect(); return { x: Math.round(r.left * 10) / 10, w: Math.round(r.width * 10) / 10 } }
      const group = document.querySelector('[data-capture-root] > div button[aria-expanded] svg.lucide-chevron-right')
      const card = document.querySelector('[data-testid="recovery-card-toggle"] svg.lucide-chevron-right')
      return { group: box(group), card: box(card) }
    })
    const aligned = m.group.x === m.card.x && m.group.w === m.card.w
    console.log(`${theme}: tool-group chevron x=${m.group.x} w=${m.group.w} | recovery chevron x=${m.card.x} w=${m.card.w} | ${aligned ? 'aligned' : 'MISALIGNED'}`)
    if (expectAligned && !aligned) failed = true
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })
    console.log(`wrote ${OUT}/${theme}.png`)
    await context.close()
  }
} finally {
  await browser.close()
  await vite.close()
}
process.exit(failed ? 1 : 0)
