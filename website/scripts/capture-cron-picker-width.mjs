/**
 * Screenshots + MEASUREMENTS for the schedule row's picker widths.
 *
 * Drives website/capture/cron-picker-width.html, which mounts the REAL JobForm
 * inside the REAL Schedule dialog. The defect is a layout fact jsdom cannot
 * see: `ui/select.tsx` draws the popup at exactly the trigger's width, and a
 * picker whose wrapper shrink-wraps takes the width of its SELECTED label — so
 * 'UTC' (or 'days') sized the whole list.
 *
 * Two modes, each ASSERTING a definite state before it writes a frame, so no
 * frame can document a state it does not show:
 *   --mode=before  expects the defect  (popup narrower than its longest row,
 *                  ≥1 row clipped) and writes before-*.png
 *   --mode=after   expects the fix     (popup ≥ the floor, 0 rows clipped)
 *                  and writes after-*.png
 *
 * Frames: 01-timezone (UTC selected) dark + light at 1280px; 03-narrow-{390,320}
 * proves the min-width floor still fits the dialog at the two widths
 * narrow-viewport.md pins.
 *
 * SWEEP_LOCALES=1 adds a report-only sweep of the neighbouring interval-unit
 * picker across all 12 catalogs. That picker has the same shrink-wrapping
 * wrapper, and the sweep is why it was left alone: every unit label fits its
 * own popup in every locale, so a floor there would be a guess, not a fix.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort     # in another shell
 *   node scripts/capture-cron-picker-width.mjs --mode=after \
 *        http://127.0.0.1:6831 ../temp-screenshots/cron-picker-width
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const args = process.argv.slice(2)
const mode = (args.find(a => a.startsWith('--mode=')) || '--mode=after').split('=')[1]
const rest = args.filter(a => !a.startsWith('--'))
const BASE = rest[0] || 'http://127.0.0.1:6831'
const OUT = rest[1] || '../temp-screenshots/cron-picker-width'
mkdirSync(OUT, { recursive: true })

// The floors the fix states, and the longest label each list must be able to
// show. Kept here so a measurement is checked against the SHIPPED number.
const TZ_FLOOR = 200

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${(ok ? 'OK  ' : 'MISMATCH')}  ${name}  ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(scene, theme, viewport = { width: 1280, height: 900 }, extra = '') {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2 })
  // Gateway-free: answer the real /api calls JobForm makes (models list).
  // Predicate on pathname — a **/api/** glob would also swallow vite's own
  // /src/api/client.ts module and break boot.
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const p = new URL(route.request().url()).pathname
    const isList = /models|agents|sessions|crons|skills|commands/.test(p)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/cron-picker-width.html?scene=${scene}&theme=${theme}${extra}`)
  // The dialog is a PORTAL, so the harness's own root div is empty and never
  // becomes visible — wait for the real dialog surface instead.
  await page.waitForSelector('[role="dialog"]')
  return page
}

/** Open one picker and measure the popup the browser actually painted.
 *  Takes a LOCATOR, not a name: the locale sweep has to reach the same picker
 *  in catalogs where its aria-label is translated. */
async function measure(page, trigger) {
  await trigger.waitFor()
  await trigger.click()
  await page.waitForSelector('[role="option"]')
  await page.waitForTimeout(250) // let the open animation settle before measuring
  return page.evaluate(() => {
    const popup = document.querySelector('[data-radix-popper-content-wrapper]')?.firstElementChild
    const rows = [...document.querySelectorAll('[role="option"]')]
    const clipped = rows
      // A row whose content is wider than its box is clipped by the popup's
      // own `overflow-hidden` — the user cannot read it.
      .filter(r => r.scrollWidth - r.clientWidth > 1)
      .map(r => r.textContent.trim())
    const widest = Math.max(...rows.map(r => r.scrollWidth))
    const box = popup?.getBoundingClientRect()
    return {
      popupWidth: Math.round(box?.width ?? 0),
      popupLeft: Math.round(box?.left ?? 0),
      popupRight: Math.round(box?.right ?? 0),
      rows: rows.length,
      clipped,
      widestRowContent: Math.round(widest),
      viewport: window.innerWidth,
    }
  })
}

const scenes = [
  { key: '01-timezone', scene: 'weekly', label: 'Timezone', floor: TZ_FLOOR },
]

for (const { key, scene, label, floor } of scenes) {
  for (const theme of ['dark', 'light']) {
    const page = await newPage(scene, theme)
    const m = await measure(page, page.getByRole('combobox', { name: label }))
    const detail = `popup=${m.popupWidth}px rows=${m.rows} widestRow=${m.widestRowContent}px clipped=${m.clipped.length}${m.clipped.length ? ` [${m.clipped.slice(0, 3).join(', ')}…]` : ''}`
    // The user-visible property is that every row FITS, so that is what the
    // after-mode assertion is; the floor is checked too, with a 1px tolerance
    // because the dialog's centering transform can render the trigger (and the
    // popup that mirrors it) a fraction under a whole pixel.
    const ok = mode === 'after'
      ? m.clipped.length === 0 && m.popupWidth >= m.widestRowContent && m.popupWidth >= floor - 1
      : m.clipped.length > 0 && m.popupWidth < m.widestRowContent
    if (check(`${mode}-${key}-${theme}`, ok, detail)) {
      await page.screenshot({ path: `${OUT}/${mode}-${key}-${theme}.png` })
    }
    await page.close()
  }
}

// ---- 03: the floor must still FIT the dialog at the widths narrow-viewport.md
// pins. DialogContent is w-[calc(100%-4rem)] with px-5 body padding, so the
// content column is (vw - 64 - 40)px: 286px at 390, 216px at 320. A floor wider
// than that column would overflow — this is the check PR #3079's rebuttal makes
// mandatory for any min-width floor.
for (const width of [390, 320]) {
  const page = await newPage('weekly', 'dark', { width, height: 760 })
  const m = await measure(page, page.getByRole('combobox', { name: 'Timezone' }))
  const inView = m.popupLeft >= 0 && m.popupRight <= width
  const detail = `vw=${width} popup=${m.popupWidth}px left=${m.popupLeft} right=${m.popupRight} clipped=${m.clipped.length} inViewport=${inView}`
  const ok = mode === 'after' ? inView && m.clipped.length === 0 : true
  if (check(`${mode}-03-narrow-${width}`, ok, detail)) {
    await page.screenshot({ path: `${OUT}/${mode}-03-narrow-${width}.png` })
  }
  await page.close()
}

// ---- 04: locale sweep for the interval-unit picker. Its popup is sized by the
// SELECTED unit, and which unit is shortest relative to its siblings is
// catalog-dependent — Italian's 'ore' has to hold 'giorni', Japanese's '日' has
// to hold '時間'. Reported, never asserted: this sweep exists to decide whether
// that picker needs a floor at all, and the answer belongs in the PR, not in a
// pass/fail gate.
if (process.env.SWEEP_LOCALES === '1') {
  console.log('\n-- interval-unit locale sweep (popup width vs widest row) --')
  for (const lang of ['en', 'de', 'it', 'fr', 'es', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']) {
    for (const secs of [3600, 86400]) {
      const page = await newPage('interval', 'dark', { width: 1280, height: 900 }, `&lang=${lang}&secs=${secs}`)
      // Positional, not by name: the aria-label is translated per catalog. The
      // interval row's comboboxes are [schedule mode, interval unit].
      const m = await measure(page, page.getByRole('combobox').nth(1)).catch(() => null)
      if (m) {
        const unit = secs === 3600 ? 'hours' : 'days'
        console.log(`  ${lang.padEnd(6)} ${unit.padEnd(6)} popup=${String(m.popupWidth).padStart(3)}px widestRow=${String(m.widestRowContent).padStart(3)}px clipped=${m.clipped.length}${m.clipped.length ? ` [${m.clipped.join(', ')}]` : ''}`)
      } else {
        console.log(`  ${lang.padEnd(6)} ${secs}  (picker not found — catalog may not translate the aria-label)`)
      }
      await page.close()
    }
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
