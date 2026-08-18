/**
 * Screenshot + assertion runner for the #4408 focus-ring fix, reusing
 * capture/subagent-completion-overflow.html (the card fixture is identical;
 * only the focus state differs).
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *   node scripts/capture-subagent-focus-ring.mjs http://127.0.0.1:6816 \
 *     ../temp-screenshots/subagent-focus-ring
 *
 * The frames are evidence, but the ASSERTIONS are the point: in `after` the
 * keyboard-focused body must paint a non-none box-shadow (Tailwind's ring is
 * box-shadow) with outline suppressed (`solid` transparent); in `before`
 * (ring=off, the pre-fix focus state) it must paint no shadow and the UA
 * `auto` outline — the clipped hairline that WAS the pre-fix indicator. A run
 * that photographs the wrong state exits nonzero instead of emitting a
 * misleading image.
 *
 * Focus is driven by KEYBOARD Tab, not element.focus(): :focus-visible only
 * matches keyboard-initiated focus, and keyboard users are who WCAG 2.4.7
 * protects.
 *
 * The runner only ever drives scene=after (the fixture's `scene=before`
 * containment hook and this `ring=off` hook are never composed): the focus
 * defect and its evidence are independent of the containment defect.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6816'
const OUT = process.argv[3] || '../temp-screenshots/subagent-focus-ring'

const BODY = '[data-testid="subagent-completion-body"]'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  for (const ring of ['before', 'after']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 1100 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(String(e)))

    const name = `${theme}-${ring}.png`
    try {
      // 'load' rather than 'networkidle': a Vite dev server's HMR socket never
      // goes network-idle. The selector waits below are the readiness signal.
      const ringParam = ring === 'before' ? '&ring=off' : ''
      await page.goto(
        `${BASE}/capture/subagent-completion-overflow.html?scene=after&theme=${theme}${ringParam}`,
        { waitUntil: 'load' },
      )
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.waitForSelector('[data-testid="subagent-completion-card"]', { timeout: 10000 })
      // A mid-wave chunk's card opens FOLDED, so expand it the way a reader would.
      const toggle = page.locator('[data-testid="subagent-completion-card"] button[aria-expanded="false"]')
      if (await toggle.count()) await toggle.click()
      await page.waitForSelector(BODY, { timeout: 10000 })
      await page.waitForTimeout(300)

      // Anchor the Tab origin: focus the expanded toggle explicitly, then Tab
      // to the body. Programmatically focusing the PREVIOUS element does not
      // affect the modality of the Tab-driven move, so :focus-visible still
      // matches on the body — and the run cannot silently Tab off whatever
      // element a future fixture change happens to leave focused.
      await page.locator('[data-testid="subagent-completion-card"] button[aria-expanded="true"]').focus()
      await page.keyboard.press('Tab')
      const m = await page.evaluate(sel => {
        const body = document.querySelector(sel)
        const cs = getComputedStyle(body)
        return {
          focused: document.activeElement === body,
          matchesFocusVisible: body.matches(':focus-visible'),
          boxShadow: cs.boxShadow,
          outline: cs.outlineStyle,
        }
      }, BODY)

      await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

      let frameFailed = 0
      if (!m.focused) {
        frameFailed++
        console.error(`FAIL ${name}: Tab did not land focus on the digest body`)
      }
      if (!m.matchesFocusVisible) {
        frameFailed++
        console.error(`FAIL ${name}: focused body does not match :focus-visible`)
      }
      const paintsRing = m.boxShadow !== 'none'
      if (ring === 'after' && !paintsRing) {
        frameFailed++
        console.error(`FAIL ${name}: focused body paints no ring (box-shadow: ${m.boxShadow})`)
      }
      if (ring === 'after' && m.outline !== 'solid') {
        frameFailed++
        console.error(`FAIL ${name}: expected suppressed (transparent solid) outline, got ${m.outline}`)
      }
      if (ring === 'before' && paintsRing) {
        frameFailed++
        console.error(`FAIL ${name}: 'before' frame paints a ring — neutralization missed`)
      }
      if (ring === 'before' && m.outline !== 'auto') {
        frameFailed++
        console.error(`FAIL ${name}: 'before' frame lost the UA outline (got ${m.outline}) — the frame would understate the pre-fix rendering`)
      }
      if (errors.length) {
        frameFailed++
        console.error(`FAIL ${name}: page errors: ${errors.join(' | ')}`)
      }
      if (frameFailed) failed += frameFailed
      else console.log(`OK ${name} (focused=${m.focused}, ring=${paintsRing ? 'painted' : 'none'})`)
    } catch (e) {
      failed++
      console.error(`FAIL ${name}: ${e}`)
    } finally {
      await ctx.close()
    }
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
