/**
 * Screenshots of the delivery-warning strip, dark and light.
 *
 * Drives the ISOLATED capture entry (website/capture/delivery-warning-strip.html), which mounts the
 * real DeliveryWarningStrip against the real stylesheet and theme tokens.
 *
 * The run is SELF-CHECKING: it counts what the component rendered and FAILS unless the caption,
 * the exit and the stated consequence each render once. A scene that rendered the wrong thing
 * exits non-zero instead of emitting a misleading image.
 *
 * Usage (one shell for the server, one for the shot):
 *   npx vite --host 127.0.0.1 --port 5599 --strictPort
 *   node scripts/capture-delivery-warning-strip.mjs http://127.0.0.1:5599 \
 *     ../temp-screenshots/delivery-warning-strip
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:5599'
const OUT = process.argv[3] || '../temp-screenshots/delivery-warning-strip'
mkdirSync(OUT, { recursive: true })

const WANT = { rows: 1, captions: 1, dismisses: 1, notes: 1 }

const run = async () => {
  const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '' } })
  let failed = 0
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 760, height: 620 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(`${BASE}/capture/delivery-warning-strip.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    try {
      await page.waitForFunction(
        (n) => document.querySelectorAll('[data-capture-row]').length >= n,
        WANT.rows,
        { timeout: 15000 },
      )
    } catch {
      console.error(`  FAIL ${theme}: rows did not render` + (errors.length ? ` (${errors[0]})` : ''))
      failed += 1
      await ctx.close()
      continue
    }
    const got = await page.evaluate(() => {
      const root = document.querySelector('[data-capture-root]')
      const count = (sel) => root.querySelectorAll(sel).length
      return {
        rows: count('[data-capture-row]'),
        captions: count('[role="status"]'),
        dismisses: count('[data-testid="delivery-exit-dismiss"]'),
        notes: count('p[id^="capture-note-"]'),
        captionText: [...root.querySelectorAll('[role="status"]')].map((n) => n.textContent.trim()),
      }
    })
    const bad = Object.keys(WANT).filter((k) => got[k] !== WANT[k])
    if (bad.length || errors.length) {
      console.error(`  FAIL ${theme}: ${bad.map((k) => `${k}=${got[k]} want ${WANT[k]}`).join(', ')}` +
        (errors.length ? ` (${errors[0]})` : ''))
      failed += 1
    } else {
      const file = `${OUT}/strip-${theme}.png`
      await page.locator('[data-capture-root]').screenshot({ path: file })
      console.log(`  ok ${theme}: ${file}`)
      console.log(`     captions: ${got.captionText.join(' | ')}`)
    }
    await ctx.close()
  }
  await browser.close()
  process.exit(failed ? 1 : 0)
}

run()
