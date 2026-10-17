/** Real-browser evidence for the crewmate bubble: no author line (#16617), no
 * "Steered" chip (#17838).
 *
 * Drives website/capture/crewmate-run.html (real ChatMessageList + crewmate
 * renderers, last reply carrying a `[STEERING …]` ack) per theme and asserts,
 * on the AFTER tree, that no `crewmate-author` row and no avatar gutter precede
 * any bubble, and that no "Steered" chip closes one while the raw marker is
 * never shown. Pass --expect-author / --expect-chip to assert the OPPOSITE on a
 * tree that predates the respective fix, which is how the "before" frame of a
 * PR body is taken from the same script. Since #17839 the AFTER tree is also
 * held to the iMessage colouring (user bubble = accent, crewmate bubble =
 * --bg-hover, code and links inside the accent bubble on --accent-fg);
 * --expect-author marks a pre-#17839 tree too and asserts the neutral user
 * bubble instead.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6882 --strictPort            # in website/
 *   node scripts/capture-crewmate-run.mjs http://127.0.0.1:6882 ../temp-screenshots/crewmate-run after
 *   node scripts/capture-crewmate-run.mjs http://127.0.0.1:6881 ../temp-screenshots/crewmate-run before --expect-chip
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6882'
const OUT = resolve(process.argv[3] || '../temp-screenshots/crewmate-run')
const TAG = process.argv[4] || 'after'
const expectAuthor = process.argv.includes('--expect-author')
const expectChip = process.argv.includes('--expect-chip')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 640 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/crewmate-run.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-testid="crewmate-message"]'); await page.waitForTimeout(500)
  const rows = page.locator('[data-testid="crewmate-message"]')
  check(`[${theme}/${TAG}] three crewmate messages drawn`, await rows.count() === 3)
  const authors = await page.locator('[data-testid="crewmate-author"]').count()
  const gutters = await rows.evaluateAll(els => els.filter(e => Array.from(e.querySelectorAll('*')).some(c => /pl-\[38px\]/.test(c.className))).length)
  if (expectAuthor) {
    check(`[${theme}/${TAG}] author line on both run openers (pre-#16617)`, authors === 2)
    check(`[${theme}/${TAG}] avatar gutter under every bubble (pre-#16617)`, gutters === 3)
  } else {
    check(`[${theme}/${TAG}] no author line on any message`, authors === 0)
    check(`[${theme}/${TAG}] no avatar gutter under any bubble`, gutters === 0)
  }
  // iMessage pairing (#17839): user = accent fill, crewmate = --bg-hover gray, no border.
  // Compared as COMPUTED colours against the theme's own tokens, so a theme change cannot pass by luck.
  const colours = await page.evaluate(() => {
    const root = document.querySelector('[data-capture-root]')
    const probe = v => { const d = document.createElement('div'); d.style.background = v; root.appendChild(d); const c = getComputedStyle(d).backgroundColor; d.remove(); return c }
    const user = getComputedStyle(document.querySelector('.user-bubble'))
    const code = document.querySelector('.user-bubble :not(pre)>code')
    const link = document.querySelector('.user-bubble a')
    const ai = getComputedStyle(document.querySelector('[data-testid="crewmate-message"] .message-bubble'))
    const aiCode = document.querySelector('[data-testid="crewmate-message"] .message-bubble :not(pre)>code')
    const quote = document.querySelector('.user-bubble [data-testid="quote-card-sent"]')
    const quoteBar = quote && quote.querySelector('span[aria-hidden]')
    const quoteExcerpt = quote && quote.querySelector('.line-clamp-2')
    return {
      quoteBar: quoteBar && getComputedStyle(quoteBar).backgroundColor,
      quoteExcerpt: quoteExcerpt && getComputedStyle(quoteExcerpt).color,
      quoteBorder: quote && getComputedStyle(quote).borderTopColor,
      accent: probe('var(--accent)'), accentFg: probe('var(--accent-fg)'), hover: probe('var(--bg-hover)'),
      userBg: user.backgroundColor, userFg: user.color,
      codeFg: code && getComputedStyle(code).color, linkFg: link && getComputedStyle(link).color,
      aiBg: ai.backgroundColor, aiBorder: ai.borderTopWidth,
      aiCodeBg: aiCode && getComputedStyle(aiCode).backgroundColor,
    }
  })
  if (expectAuthor) {
    check(`[${theme}/${TAG}] user bubble still neutral (pre-#17839)`, colours.userBg !== colours.accent)
  } else {
    check(`[${theme}/${TAG}] user bubble is the theme accent`, colours.userBg === colours.accent && colours.userFg === colours.accentFg)
    check(`[${theme}/${TAG}] inline code and link inside it sit on --accent-fg`, colours.codeFg === colours.accentFg && colours.linkFg === colours.accentFg)
    check(`[${theme}/${TAG}] crewmate bubble is the --bg-hover gray with no border`, colours.aiBg === colours.hover && colours.aiBorder === '0px')
    // kiro-light paints inline code with var(--bg-hover), the fill's own colour: the bubble must move
    // that one step off the fill or the patch disappears (Design review R3 on #17918).
    check(`[${theme}/${TAG}] inline code in a crewmate reply sits on a patch distinct from the fill`, !!colours.aiCodeBg && colours.aiCodeBg !== colours.aiBg && colours.aiCodeBg !== 'rgba(0, 0, 0, 0)')
    // The quote card inside the accent bubble reads the REDEFINED tokens: its bar is the text
    // colour (not accent-on-accent), its excerpt and border are accent-fg mixes, never the page's gray.
    const isFgMix = c => !!c && c !== colours.accent && c !== 'rgba(0, 0, 0, 0)' && /\/ 0\.\d/.test(c)
    check(`[${theme}/${TAG}] quote card bar inside the accent bubble is the text colour, not the accent`, colours.quoteBar === colours.accentFg)
    check(`[${theme}/${TAG}] quote card excerpt and border derive from --accent-fg`, isFgMix(colours.quoteExcerpt) && isFgMix(colours.quoteBorder))
  }
  const chips = await page.getByText('Steered', { exact: true }).count()
  check(`[${theme}/${TAG}] raw [STEERING …] marker never shown`, await page.getByText('[STEERING').count() === 0)
  if (expectChip) {
    check(`[${theme}/${TAG}] "Steered" chip closes the last reply (pre-#17838)`, chips === 1)
  } else {
    check(`[${theme}/${TAG}] no "Steered" chip on any reply`, chips === 0)
  }
  check(`[${theme}/${TAG}] no page errors`, errors.length === 0)
  await page.screenshot({ path: resolve(OUT, `${theme}-${TAG}.png`) })
  await ctx.close()
}
await browser.close()
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
