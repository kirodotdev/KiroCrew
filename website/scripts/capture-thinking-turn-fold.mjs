/**
 * Screenshots of a settled multi-burst turn as drawn by TurnBlock in the
 * default "show thinking inline" mode.
 *
 * Run the SAME page twice — once against the pre-change TurnBlock (git stash of
 * website/src/pages/chat/TurnBlock.tsx) and once against the patched one — into
 * two OUT dirs to get the before/after pair a UI PR needs. The harness never
 * changes between runs, so the only variable is the component.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort    # in another shell
 *   node scripts/capture-thinking-turn-fold.mjs http://127.0.0.1:6813 ../temp-screenshots/thinking-turn-fold/after
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/thinking-turn-fold/after'
mkdirSync(OUT, { recursive: true })

const run = async () => {
  const browser = await chromium.launch()
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 900, height: 1100 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    await page.goto(`${BASE}/capture/thinking-turn-fold.html?theme=${theme}&bursts=6`)
    // Wait for the turn to mount: at least one collapsed reasoning row exists.
    await page.waitForSelector('button[aria-expanded]')
    // Let the liveness idle-window lapse so the settled label ("Thought
    // process") is what the frame shows, not the transient "Thinking".
    await page.waitForTimeout(1600)
    const root = page.locator('[data-capture-root]')
    await root.screenshot({ path: `${OUT}/turn-${theme}.png` })
    console.log(`ok  turn-${theme}.png`)
    await ctx.close()
  }
  await browser.close()
}

run().catch((e) => { console.error(e); process.exit(1) })
