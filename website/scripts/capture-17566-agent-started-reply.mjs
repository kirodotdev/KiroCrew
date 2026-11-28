/**
 * Screenshot harness: a turn the agent started on its own, in the chat pane,
 * before (main) and after, light and dark, plus a turn that wrote only a tool
 * row. Prints how many "Started by the agent" labels each frame draws and,
 * where it should draw one, whether it sits above the turn's tool-call toggle,
 * so the frame and the check come from the same render.
 *
 * Gateway-free: an in-process vite dev server serves the build-excluded capture
 * entry (capture/agent-started-reply.html).
 *
 * Usage: node scripts/capture-17566-agent-started-reply.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || 'temp-screenshots/17566-agent-started-reply'
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
  for (const state of ['after', 'before', 'tool-only']) {
    for (const theme of ['light', 'dark']) {
      // Tall enough that neither state scrolls: a scrolled pane pins the prompt and fades the top row.
      const context = await browser.newContext({ deviceScaleFactor: 2, viewport: { width: 820, height: 1000 } })
      const page = await context.newPage()
      page.on('pageerror', e => { console.log(`FAIL pageerror: ${e.message}`); failed = true })
      await page.goto(`${BASE}/capture/agent-started-reply.html?state=${state}&theme=${theme}`, { waitUntil: 'domcontentloaded' })
      await page.waitForSelector('[data-role="assistant"]', { timeout: 60_000 })
      await page.evaluate(() => document.fonts.ready)
      await page.waitForTimeout(1500)
      const m = await page.evaluate(() => {
        const labels = [...document.querySelectorAll('[data-testid="agent-started-label"]')]
        const toggle = [...document.querySelectorAll('button[aria-expanded]')].find(b => /tool call/i.test(b.textContent ?? ''))
        const top = el => (el ? Math.round(el.getBoundingClientRect().top) : null)
        return { labels: labels.length, labelTop: top(labels[0]), toggleTop: top(toggle) }
      })
      const headed = m.labelTop !== null && m.toggleTop !== null && m.labelTop < m.toggleTop
      const labelled = state !== 'before'
      console.log(`${state} ${theme}: labels=${m.labels} label y=${m.labelTop} tool-call toggle y=${m.toggleTop}${labelled ? ` | ${headed ? 'label heads the turn' : 'LABEL NOT ABOVE THE TOOL ROWS'}` : ''}`)
      if (labelled ? m.labels !== 1 || !headed : m.labels !== 0) failed = true
      // The transcript only: the composer's banners are a capture without a gateway behind it.
      const root = await page.locator('[data-capture-root]').boundingBox()
      const last = await page.locator('[data-role="assistant"]').last().boundingBox()
      const clip = { x: root.x, y: root.y, width: root.width, height: last.y + last.height + 24 - root.y }
      await page.screenshot({ path: `${OUT}/${state}-${theme}.png`, clip })
      console.log(`wrote ${OUT}/${state}-${theme}.png`)
      await context.close()
    }
  }
} finally {
  await browser.close()
  await vite.close()
}
process.exit(failed ? 1 : 0)
