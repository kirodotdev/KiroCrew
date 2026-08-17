/**
 * Screenshot + style probe for capture/transcript-row-style.html.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *   node scripts/capture-transcript-row-style.mjs http://127.0.0.1:6816 \
 *     ../temp-screenshots/transcript-row-style
 *
 * The probe is the deliverable, not the image: it walks every rendered row and
 * reports the four axes the style proposal argues over -- box offset from the
 * column text edge, the row's own horizontal padding, its radius, and its font
 * size -- then prints a frequency table so the majority and the outliers are
 * read off measurements rather than asserted from reading source.
 *
 * The only hard assertion is alignment: every row's box must sit ON the column
 * text edge. Padding, radius and font size are DELIBERATELY not asserted -- they
 * are the spread being proposed about, so pinning them here would prejudge the
 * decision the frames exist to inform.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6816'
const OUT = process.argv[3] || '../temp-screenshots/transcript-row-style'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    // Tall enough for the whole inventory; 900x2 = 1800px wide keeps the frame
    // under the 2000px-per-edge ceiling, so height is what has to give.
    viewport: { width: 900, height: 1000 },
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  try {
    await page.goto(`${BASE}/capture/transcript-row-style.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    await page.waitForTimeout(900)

    const rows = await page.evaluate(() => {
      const root = document.querySelector('[data-capture-root]')
      const rb = root.getBoundingClientRect()
      const textEdge = rb.x + (rb.width - 800) / 2 + 20
      // Host row wrappers are the direct children of the list that carry the
      // column geometry; the row's own box is what sits inside one.
      const wrappers = [...root.querySelectorAll('[style*="--mc-content-width"]')]
        .filter(el => el.className.includes('px-5'))
      const seen = new Set()
      const out = []
      for (const w of wrappers) {
        const el = w.firstElementChild
        if (!el || seen.has(el)) continue
        seen.add(el)
        const b = el.getBoundingClientRect()
        if (b.width === 0 || b.height === 0) continue
        const cs = getComputedStyle(el)
        // Walk to the first descendant that actually paints a surface, which is
        // the "card" a reader perceives -- some rows put it one level down.
        let card = el
        for (let i = 0; i < 3; i++) {
          const paints = getComputedStyle(card).borderTopWidth !== '0px'
            || !['rgba(0, 0, 0, 0)', 'transparent'].includes(getComputedStyle(card).backgroundColor)
          if (paints) break
          if (!card.firstElementChild) break
          card = card.firstElementChild
        }
        const ccs = getComputedStyle(card)
        // Font size must be read off the element that actually HOLDS TEXT, not
        // off the card box: a container usually inherits the page's 14px while
        // its label is 13px, so measuring the box reports the wrong axis and is
        // not comparable to the `text-[13px]` written in source.
        const textEl = (() => {
          const walk = document.createTreeWalker(card, NodeFilter.SHOW_TEXT)
          let n
          while ((n = walk.nextNode())) {
            if ((n.textContent || '').trim() && n.parentElement) return n.parentElement
          }
          return card
        })()
        out.push({
          tag: el.tagName.toLowerCase(),
          testid: el.getAttribute('data-testid') || card.getAttribute('data-testid') || '',
          dx: Math.round(b.x - textEdge),
          pad: ccs.paddingLeft,
          radius: ccs.borderTopLeftRadius,
          font: getComputedStyle(textEl).fontSize,
        })
      }
      return out
    })

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })

    let frameFailed = 0
    for (const r of rows) {
      if (r.dx !== 0) {
        frameFailed++
        console.error(`FAIL ${theme}: row ${r.testid || r.tag} box starts at ${r.dx}px, not on the column text edge`)
      }
    }
    if (errors.length) {
      frameFailed++
      console.error(`FAIL ${theme}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
    }
    failed += frameFailed

    if (!frameFailed) {
      console.log(`\nok   ${theme}.png — ${rows.length} rows`)
      console.log('     ' + 'row'.padEnd(28) + 'pad'.padEnd(8) + 'radius'.padEnd(9) + 'font')
      for (const r of rows) {
        console.log('     ' + (r.testid || r.tag).padEnd(28) + r.pad.padEnd(8) + r.radius.padEnd(9) + r.font)
      }
      for (const axis of ['pad', 'radius', 'font']) {
        const counts = {}
        for (const r of rows) counts[r[axis]] = (counts[r[axis]] || 0) + 1
        const spread = Object.entries(counts).sort((a, b) => b[1] - a[1])
          .map(([v, n]) => `${v}×${n}`).join('  ')
        console.log(`     ${axis.padEnd(7)} ${spread}`)
      }
    }
  } catch (err) {
    failed++
    console.error(`FAIL ${theme}: ${err.message}`)
  }
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error(`\n${failed} assertion(s) failed — the frames do not show the state they claim.`)
  process.exit(1)
}
