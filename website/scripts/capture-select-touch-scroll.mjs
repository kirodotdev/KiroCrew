/**
 * Evidence for the Settings dropdown that a phone cannot scroll.
 *
 * Drives the ISOLATED capture entry (website/capture/select-touch-scroll.html),
 * which mounts the REAL `SettingsSelect` with the STT language list (~40 BCP-47
 * codes) — the row the defect was reported on.
 *
 * Three scenes, because the change is conditional on the POINTER TYPE and a
 * single shot cannot show that:
 *
 *   before-popup-390    a 390px viewport with a FINE pointer — the Radix popup a
 *                       phone used to get: capped at 240px, 7 of 41 rows visible,
 *                       the rest behind a drag gesture iOS Safari does not
 *                       reliably deliver. Rendering is pointer-independent, so
 *                       this is pixel-identical to what the phone showed.
 *   after-native-390    the same viewport with a COARSE pointer — the native
 *                       control. Its list is drawn by the OS and is deliberately
 *                       absent from the frame: that is the fix, not a capture
 *                       gap. Chromium's own picker is not part of the page.
 *   desktop-unchanged   1280px, fine pointer — the Radix popup, to show the
 *                       pointer-device path is untouched.
 *
 * Each scene also asserts which control rendered, so a regression that quietly
 * routes the wrong way fails here rather than in review.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-select-touch-scroll.mjs http://127.0.0.1:6831 ../temp-screenshots/select-touch
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/select-touch'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'before-popup-390', w: 390, h: 844, touch: false, open: true, expect: 'BUTTON' },
  { name: 'after-native-390', w: 390, h: 844, touch: true, open: false, expect: 'SELECT' },
  { name: 'desktop-unchanged', w: 1280, h: 900, touch: false, open: true, expect: 'BUTTON' },
]

const browser = await chromium.launch()

for (const scene of SCENES) {
  const ctx = await browser.newContext({
    viewport: { width: scene.w, height: scene.h },
    deviceScaleFactor: 2,
    isMobile: scene.touch,
    hasTouch: scene.touch,
  })
  const page = await ctx.newPage()
  await page.goto(`${BASE}/capture/select-touch-scroll.html?theme=light&filler=120`)

  // `select, [role="combobox"]` covers both paths: the native control has an
  // implicit combobox role and no role ATTRIBUTE for a CSS selector to match.
  const control = page.locator('select, [role="combobox"]').first()
  await control.waitFor({ timeout: 20000 })

  const tag = await control.evaluate(el => el.tagName)
  if (tag !== scene.expect) throw new Error(`${scene.name}: expected <${scene.expect}>, got <${tag}>`)

  if (scene.open) {
    await control.click()
    await page.waitForSelector('[data-radix-select-viewport]', { timeout: 10000 })
    await page.waitForTimeout(400)
    const m = await page.evaluate(() => window.__measure())
    console.log(`${scene.name.padEnd(18)} <${tag}> popup ${m.rect.h}px · ${m.items} rows · ${Math.round(m.clientHeight / 32)} visible`)
  } else {
    const opts = await control.evaluate(el => el.querySelectorAll('option').length)
    console.log(`${scene.name.padEnd(18)} <${tag}> ${opts} options · list drawn by the OS`)
  }

  await page.screenshot({ path: `${OUT}/${scene.name}.png` })
  await ctx.close()
}

await browser.close()
