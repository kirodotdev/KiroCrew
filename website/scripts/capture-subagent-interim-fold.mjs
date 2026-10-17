/**
 * Screenshots of a sub-agent fan-out transcript as drawn by the real grouping
 * pass + TurnBlock: the interim per-completion summaries folded behind one
 * toggle, versus the same rows unfolded.
 *
 * The `?fold=0|1` switch on the harness page is what produces the before/after
 * pair, so both frames come from ONE code state and differ only in whether the
 * fold is applied (the harness header explains why that is faithful here).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort    # in another shell
 *   node scripts/capture-subagent-interim-fold.mjs http://127.0.0.1:6814 ../temp-screenshots/subagent-interim-fold
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/subagent-interim-fold'
mkdirSync(OUT, { recursive: true })

const run = async () => {
  const browser = await chromium.launch()
  for (const theme of ['dark', 'light']) {
    for (const [name, fold] of [['before', '0'], ['after', '1']]) {
      const ctx = await browser.newContext({ viewport: { width: 900, height: 560 }, deviceScaleFactor: 2 })
      const page = await ctx.newPage()
      await page.goto(`${BASE}/capture/subagent-interim-fold.html?theme=${theme}&fold=${fold}`)
      // Wait for the transcript to mount: the synthesis answer is always drawn.
      await page.waitForSelector('text=/Renderer crash/')
      // Let the collapse animation settle so the frame is not mid-transition.
      await page.waitForTimeout(600)
      const root = page.locator('[data-capture-root]')
      await root.screenshot({ path: `${OUT}/${name}-${theme}.png` })
      console.log(`ok  ${name}-${theme}.png`)
      await ctx.close()
    }
  }
  await browser.close()
}

run().catch((e) => { console.error(e); process.exit(1) })
