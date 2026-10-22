/** Real-browser evidence for line-anchored comments on artifact code blocks.
 *
 * Starts Vite in-process, drives the ISOLATED capture entry
 * (website/capture/artifact-code-comments.html, the REAL `ArtifactDetailPage`
 * over a fixture fetch boundary) and asserts, per theme:
 *
 *  1-chips:     two code threads render as chips inside the Pierre block — the
 *               read one outlined, the unread one filled.
 *  2-gutter:    dragging across line numbers 3..5 selects the range and the
 *               hovered line shows the gutter add-comment button.
 *  3-composer:  the gutter button opens the page's comment composer.
 *  4-posted:    the post carries a SOURCE-offset anchor for lines 3..5, and the
 *               new thread comes back as a third chip.
 *
 * Usage (from website/):
 *   node scripts/capture-artifact-code-comments.mjs [outDir]
 * Set PW_LD_LIBRARY_PATH when the host lacks the browser's shared libraries.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../.github/screenshots/pr-8360'
mkdirSync(OUT, { recursive: true })
const PORT = 6871

const server = await createServer({ server: { host: '127.0.0.1', port: PORT, strictPort: true } })
await server.listen()
const { LD_LIBRARY_PATH: _mise, ...env } = process.env
if (process.env.PW_LD_LIBRARY_PATH) env.LD_LIBRARY_PATH = process.env.PW_LD_LIBRARY_PATH
const browser = await chromium.launch({ env })

let failures = 0
const check = (label, ok) => { console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

try {
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })
    page.on('pageerror', e => { console.error(`[${theme}] pageerror:`, e.message); failures++ })
    await page.goto(`http://127.0.0.1:${PORT}/capture/artifact-code-comments.html?theme=${theme}`, { waitUntil: 'networkidle', timeout: 180000 })
    const chips = page.locator('[data-mc-code-chip]')
    await chips.nth(1).waitFor({ timeout: 30000 })
    await page.waitForTimeout(500)

    // 1 — read chip outlined, unread chip filled.
    const bg = await chips.evaluateAll(els => els.map(el => getComputedStyle(el).backgroundColor))
    check(`[${theme}] two chips render`, bg.length === 2)
    check(`[${theme}] read chip outlined, unread chip filled`, bg[0] === 'rgba(0, 0, 0, 0)' && bg[1] !== 'rgba(0, 0, 0, 0)')
    await page.screenshot({ path: `${OUT}/${theme}-1-chips.png` })

    // 2 — drag across line numbers 3..5, then hover line 4.
    const num = n => page.locator(`[data-column-number="${n}"]`).first()
    const a = await num(3).boundingBox()
    const b = await num(5).boundingBox()
    await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2)
    await page.mouse.down()
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 6 })
    await page.mouse.up()
    const mid = await num(4).boundingBox()
    await page.mouse.move(mid.x + mid.width / 2, mid.y + mid.height / 2)
    await page.mouse.move(mid.x + mid.width / 2 + 30, mid.y + mid.height / 2, { steps: 3 })
    const gutter = page.locator('[data-testid="artifact-code"] button, button').filter({ has: page.locator('svg.lucide-message-square-plus') }).first()
    await gutter.waitFor({ state: 'visible', timeout: 10000 })
    check(`[${theme}] gutter add-comment button shows on the hovered line`, await gutter.isVisible())
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${theme}-2-gutter.png` })

    // 3 — the gutter button opens the page composer.
    // A real mouse click at the button: Playwright's actionability check reads
    // the shadow HOST as the hit target for slotted content and refuses.
    const g = await gutter.boundingBox()
    await page.mouse.click(g.x + g.width / 2, g.y + g.height / 2)
    const input = page.getByLabel('Comment on the selected text')
    await input.waitFor({ state: 'visible', timeout: 10000 })
    await input.fill('Pull the loop body into a named helper.')
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${theme}-3-composer.png` })

    // 4 — post; the anchor is lines 3..5 of the fence in source space.
    await input.press('Enter')
    await input.waitFor({ state: 'detached', timeout: 10000 })
    await chips.nth(2).waitFor({ timeout: 10000 })
    const posted = await page.evaluate(() => window.__posted)
    const anchor = posted[0]?.anchor ?? {}
    console.log(`[${theme}] posted quote: ${JSON.stringify(anchor.quote)}`)
    const src = await page.evaluate(() => fetch('/api/artifacts/retry-helper').then(r => r.json()).then(j => j.content))
    check(`[${theme}] posted anchor is the source slice of lines 3..5`,
      posted.length === 1 && typeof anchor.start_offset === 'number'
      && src.slice(anchor.start_offset, anchor.end_offset) === anchor.quote
      && anchor.quote.startsWith('  for (let i = 0') && anchor.quote.endsWith('setTimeout(r, delay))') && anchor.quote.split('\n').length === 3)
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${theme}-4-posted.png` })

    // 5 — clicking a chip opens its thread popover beside the chip.
    const chip = chips.nth(1)
    const cb = await chip.boundingBox()
    await page.mouse.click(cb.x + cb.width / 2, cb.y + cb.height / 2)
    const pop = page.getByRole('dialog', { name: 'Comment thread' })
    await pop.waitFor({ state: 'visible', timeout: 10000 })
    const pb = await pop.boundingBox()
    check(`[${theme}] chip opens its thread`, (await pop.getByText('Add jitter so callers').count()) === 1)
    check(`[${theme}] popover sits next to the chip`, Math.abs(pb.y - (cb.y + cb.height)) < 80 || Math.abs(pb.y + pb.height - cb.y) < 80)
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${theme}-5-thread.png` })
    await page.close()
  }
} finally {
  await browser.close()
  await server.close()
}
console.log(failures ? `${failures} check(s) FAILED` : 'all checks OK')
process.exit(failures ? 1 : 0)
