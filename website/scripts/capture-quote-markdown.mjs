/** Real-browser evidence for the sent quote card rendering its quote as markdown.
 *
 * Drives website/capture/quote-message.html?scene=sent&quote=long (the real
 * UserMessage + QuoteCard over a long quoted reply with a table and a list).
 * Per theme and width (desktop 1100px, 380px side panel, 390px phone):
 *
 *   clamped   the card as sent: markdown, clamped with a fade, Show more
 *   expanded  after Show more: taller, capped near a third of the viewport
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6873 --strictPort     # in website/
 *   node scripts/capture-quote-markdown.mjs http://127.0.0.1:6873 ../temp-screenshots/quote-markdown
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6873'
const OUT = resolve(process.argv[3] || '../temp-screenshots/quote-markdown')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

const WIDTHS = [
  { name: 'desktop', viewport: { width: 1100, height: 720 }, mobile: false },
  { name: 'panel380', viewport: { width: 380, height: 780 }, mobile: false },
  { name: 'phone', viewport: { width: 390, height: 780 }, mobile: true },
]

for (const theme of ['dark', 'light']) {
  for (const w of WIDTHS) {
    const ctx = await browser.newContext({ viewport: w.viewport, deviceScaleFactor: 2, hasTouch: w.mobile, isMobile: w.mobile })
    const page = await ctx.newPage()
    const errors = []; page.on('pageerror', e => errors.push(String(e)))
    await page.goto(`${BASE}/capture/quote-message.html?theme=${theme}&scene=sent&quote=long`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-testid="quote-card-sent"]'); await page.waitForTimeout(400)
    const card = page.locator('[data-testid="quote-card-sent"]')
    await card.scrollIntoViewIfNeeded()
    const tag = `[${theme}/${w.name}]`
    const tables = await card.locator('table').count()
    check(`${tag} quoted table renders as a table`, tables === 1)
    const h0 = (await card.boundingBox()).height
    check(`${tag} clamped card is short (${Math.round(h0)}px)`, h0 < 220)
    await page.screenshot({ path: resolve(OUT, `${theme}-${w.name}-1-clamped.png`) })
    const toggle = card.locator('[data-testid="quote-card-toggle"]')
    if (await toggle.count()) {
      await toggle.click(); await page.waitForTimeout(500)
      const h1 = (await card.boundingBox()).height
      check(`${tag} Show more expands (${Math.round(h0)} -> ${Math.round(h1)}px)`, h1 > h0)
      check(`${tag} expanded card stays near a third of the viewport`, h1 < w.viewport.height * 0.33 + 120)
      check(`${tag} toggle reads Show less`, (await toggle.innerText()).trim() === 'Show less')
      const cue = await card.locator('[data-testid="quote-card-body"]').evaluate(b => ({ more: b.scrollHeight - b.clientHeight - b.scrollTop > 1, masked: b.className.includes('mask-image') }))
      check(`${tag} expanded box fades its bottom exactly while more sits below`, cue.more === cue.masked)
      await card.scrollIntoViewIfNeeded()
      await page.screenshot({ path: resolve(OUT, `${theme}-${w.name}-2-expanded.png`) })
    } else {
      check(`${tag} Show more toggle present`, false)
    }
    check(`${tag} no page errors`, errors.length === 0)
    await ctx.close()
  }
}
// A recording of Show more -> scroll -> Show less at the 380px side-panel width.
{
  const ctx = await browser.newContext({ viewport: { width: 380, height: 780 }, recordVideo: { dir: OUT, size: { width: 380, height: 780 } } })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/capture/quote-message.html?theme=dark&scene=sent&quote=long`, { waitUntil: 'networkidle' })
  const card = page.locator('[data-testid="quote-card-sent"]')
  await card.scrollIntoViewIfNeeded(); await page.waitForTimeout(800)
  await card.locator('[data-testid="quote-card-toggle"]').click(); await page.waitForTimeout(1000)
  await card.scrollIntoViewIfNeeded(); await page.waitForTimeout(600)
  await card.locator('[data-testid="quote-card-body"]').hover()
  await page.mouse.wheel(0, 400); await page.waitForTimeout(1000)
  await card.locator('[data-testid="quote-card-toggle"]').click(); await page.waitForTimeout(1000)
  const video = page.video()
  await ctx.close()
  if (video) console.log('video:', await video.path())
}
await browser.close()
console.log(failures ? `${failures} FAIL` : 'ALL OK')
process.exit(failures ? 1 : 0)
