/**
 * Screenshot runner for capture/error-notice-pages-rest-1.html (dashboard
 * error-state sweep, batch pages-rest-1).
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort
 *   node scripts/capture-error-notice-pages-rest-1.mjs http://127.0.0.1:6832 <outdir> [before|after]
 *
 * `after` (default) asserts every scene renders at least one "Ask the agent"
 * hand-off and that the legacy hand-written markers are gone; `before`, run
 * against the base branch with the same harness, asserts the opposite — so a
 * frame can never photograph the wrong tree.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/error-notice-pages-rest-1'
const MODE = process.argv[4] === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })

const THEMES = MODE === 'after' ? ['dark', 'light'] : ['dark']
const browser = await chromium.launch()
let failed = 0

for (const theme of THEMES) {
  const ctx = await browser.newContext({
    viewport: { width: 1060, height: 2800 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/error-notice-pages-rest-1.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.addStyleTag({
      content: '*, *::before, *::after { animation-duration: 0s !important;'
        + ' animation-delay: 0s !important; transition-duration: 0s !important;'
        + ' transition-delay: 0s !important; }',
    })
    await page.waitForSelector('[data-capture-root]')
    // Settled error state: the rejected fetch's reason must be on screen.
    await page.getByText('Failed to fetch: gateway unreachable').first().waitFor({ timeout: 15000 })
    // Give the remaining queries a moment to settle too (they reject synchronously).
    await page.waitForTimeout(500)
    if (MODE === 'after') {
      // HooksPage: open the failed hook's persisted last_error row (a tooltip on
      // the base branch), then run Test against the rejecting endpoint so the
      // hook-test failure notice renders too.
      await page.getByRole('button', { name: /show last error for fmt/i }).click()
      await page.getByRole('button', { name: /^test$/i }).first().click()
      await page.getByTestId('hook-test-error').waitFor({ timeout: 10000 })
      // The clicks scroll the hooks table sideways to reveal the sticky Actions
      // column; put it back so the frame shows the row from its left edge.
      await page.evaluate(() => { for (const el of document.querySelectorAll('.overflow-x-auto')) el.scrollLeft = 0 })
    }
    const handoffs = await page.getByRole('button', { name: 'Ask the agent' }).count()
    const scenes = await page.locator('[data-scene]').count()
    if (MODE === 'after') {
      if (handoffs < scenes) throw new Error(`expected a hand-off per scene (${scenes}), got ${handoffs}`)
      const legacy = await page.locator(
        '[data-scene] .text-danger\\/90, [data-scene] .bg-danger\\/10:not([role="alert"])',
      ).count()
      if (legacy) throw new Error(`legacy hand-written error markup still present (${legacy})`)
    } else if (handoffs !== 0) {
      throw new Error(`BEFORE frame unexpectedly renders ${handoffs} hand-off link(s)`)
    }
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${MODE}-${theme}.png` })
    console.log(`${MODE}-${theme}: ${scenes} scenes, ${handoffs} hand-off link(s) — OK`)
  } catch (e) {
    console.error(`${MODE}-${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
