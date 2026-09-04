/**
 * Screenshot runner for capture/notice-card-style.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort
 *   node scripts/capture-notice-card-style.mjs http://127.0.0.1:6821 <outdir>
 *
 * Captures the before/after/tones sheet in both themes at deviceScaleFactor 1,
 * element-scoped so every frame stays under 2000px on both edges.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/notice-card-style'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({
    viewport: { width: 780, height: 1400 },
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/notice-card-style.html?theme=${theme}&lang=zh-CN`, {
      waitUntil: 'networkidle',
    })
    await page.waitForSelector('[data-capture-root]', { timeout: 30000 })
    await page.waitForTimeout(600)
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)

    // Self-check, all scoped to the episode the claim is about.
    const probe = await page.evaluate(() => {
      const after = document.querySelector('[data-episode="after"]')
      const afterNotice = after?.querySelector('[data-testid="notice-card"]')
      const afterRecovery = after?.querySelector('[data-testid="recovery-card"]')
      const toneRows = [...document.querySelectorAll('[data-episode="tones"] [data-testid="notice-card"]')]
      return {
        notices: document.querySelectorAll('[data-testid="notice-card"]').length,
        recovery: document.querySelectorAll('[data-testid="recovery-card"]').length,
        // Only the two "before" rows may still show a baked-in glyph.
        infoEmoji: document.body.innerText.split('\u2139').length - 1,
        warnEmoji: document.body.innerText.split('\u26A0').length - 1,
        blockedEmoji: document.body.innerText.split('\u26D4').length - 1,
        // Width parity, measured on the SAME episode's stacked pair.
        widthDelta:
          afterNotice && afterRecovery
            ? Math.abs(
                afterNotice.getBoundingClientRect().width -
                  afterRecovery.getBoundingClientRect().width,
              )
            : NaN,
        icons: document.querySelectorAll('[data-testid="notice-card"] svg.lucide-inline').length,
        tones: toneRows.map(r => r.getAttribute('data-tone')).join(','),
        // Computed styles prove Tailwind actually compiled the recipe — DOM
        // counts alone stay green when the stylesheet is missing entirely.
        computed: (() => {
          if (!afterNotice) return null
          const card = getComputedStyle(afterNotice)
          const recovery = afterRecovery ? getComputedStyle(afterRecovery) : null
          const row = getComputedStyle(afterNotice.firstElementChild)
          const iconBox = afterNotice.querySelector('svg')?.getBoundingClientRect()
          return {
            // rounded-md resolves var(--radius-md), a theme value — assert
            // parity with the recovery card and non-zero, not a literal px.
            radius: card.borderRadius,
            recoveryRadius: recovery?.borderRadius ?? '',
            padding: `${row.paddingTop} ${row.paddingRight} ${row.paddingBottom} ${row.paddingLeft}`,
            font: `${row.fontSize}/${row.lineHeight}`,
            icon: iconBox ? `${iconBox.width}x${iconBox.height}` : '',
          }
        })(),
      }
    })
    if (probe.notices !== 4) throw new Error(`expected 4 NoticeCards, got ${probe.notices}`)
    if (probe.recovery !== 2) throw new Error(`expected 2 recovery cards, got ${probe.recovery}`)
    if (probe.infoEmoji !== 2) throw new Error(`expected 2 legacy ℹ emoji (before rows only), got ${probe.infoEmoji}`)
    if (probe.warnEmoji !== 0) throw new Error(`warn emoji leaked into a rendered row (${probe.warnEmoji})`)
    if (probe.blockedEmoji !== 0) throw new Error(`blocked emoji leaked into a rendered row (${probe.blockedEmoji})`)
    if (!(probe.widthDelta < 0.5)) throw new Error(`notice/recovery width delta ${probe.widthDelta}px`)
    if (probe.icons !== 4) throw new Error(`expected 4 lucide icons in NoticeCards, got ${probe.icons}`)
    if (probe.tones !== 'warn,blocked') throw new Error(`tones rows = ${probe.tones}, expected warn,blocked`)
    // rounded-md=6px, px-3/py-2=12px/8px, text-[13px] leading-5=13px/20px,
    // .lucide-inline 1em glyph at 13px type = 13x13.
    const c = probe.computed
    if (!c) throw new Error('computed-style probe found no AFTER notice')
    if (c.radius === '0px' || c.radius !== c.recoveryRadius)
      throw new Error(`radius ${c.radius} vs recovery ${c.recoveryRadius} (Tailwind not compiled, or drift)`)
    if (c.padding !== '8px 12px 8px 12px') throw new Error(`padding ${c.padding}, expected 8px 12px`)
    if (c.font !== '13px/20px') throw new Error(`type ${c.font}, expected 13px/20px`)
    if (c.icon !== '13x13') throw new Error(`icon box ${c.icon}, expected 13x13`)

    const el = page.locator('[data-capture-root]')
    await el.screenshot({ path: `${OUT}/notice-card-style-${theme}.png` })
    console.log(`ok ${theme}: width delta ${probe.widthDelta}px, emoji only in before rows, tones honored`)
  } catch (e) {
    failed = 1
    console.error(`FAIL ${theme}:`, e.message)
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed)
