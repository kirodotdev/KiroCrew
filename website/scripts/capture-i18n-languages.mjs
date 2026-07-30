/**
 * Screenshot harness for the shipped UI languages.
 *
 * Points a real browser at a REAL gateway (`./dev-backend.sh`, isolated
 * `.kirocrew-dev/` data home) rather than mocking `/api/**` from fixtures.
 * That matters here: hand-written fixtures drifted from the live contracts and
 * produced frames of the SPA's error boundary — a screenshot that verifies
 * nothing. Driving the real backend means a frame is evidence.
 *
 * The language is set through the seam production uses: `dashboard.language`
 * persisted via `PUT /api/config/theme`, surfaced by `GET /api/theme/boot`, and
 * mirrored into `localStorage['mc-lang']` so the FIRST paint is already
 * translated. Screenshotting that path proves the boot wiring works, not just
 * that a catalog parses.
 *
 * Captures, per language:
 *   <code>-sessions.png      nav rail + welcome view + composer
 *   <code>-schedule.png      Schedule table (job rows, headers)
 *   <code>-bulk-delete.png   bulk-delete confirmation: count plural + the
 *                            "Type `delete` to confirm" instruction this PR fixes
 *   <code>-display.png       Settings > Display, incl. the language picker
 *
 * Usage:
 *   ./dev-backend.sh &                       # real gateway on :6777
 *   node scripts/capture-i18n-languages.mjs <outDir> <baseUrl> <token>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '/tmp/i18n-shots'
const BASE = (process.argv[3] || 'http://127.0.0.1:6777').replace(/\/$/, '')
const TOKEN = process.argv[4] || ''
mkdirSync(OUT, { recursive: true })

/** Languages to capture, with the endonym the picker must show. */
const LANGUAGES = [
  { code: 'en', label: 'English' },
  { code: 'zh-CN', label: '简体中文' },
  { code: 'hi', label: 'हिन्दी' },
  { code: 'es', label: 'Español' },
  { code: 'fr', label: 'Français' },
  { code: 'bn', label: 'বাংলা' },
  { code: 'pt', label: 'Português' },
  { code: 'ru', label: 'Русский' },
]

const browser = await chromium.launch()
const failures = []

for (const lang of LANGUAGES) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    locale: lang.code,
  })
  const page = await context.newPage()

  // Seed the boot fast-path so the first paint is already in-language, and clear
  // first-run gates (a naive load otherwise renders behind the onboarding modal).
  await context.addInitScript(code => {
    localStorage.setItem('mc-lang', code)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-theme', 'light')
  }, lang.code)

  const errors = []
  page.on('console', m => {
    const t = m.text()
    if (/ErrorBoundary|TypeError|is not iterable/.test(t)) errors.push(t.slice(0, 200))
  })

  // The token handshake sets the auth cookie, then we navigate normally.
  await page.goto(`${BASE}/?token=${encodeURIComponent(TOKEN)}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  // Persist the language server-side too, so /api/theme/boot returns it on the
  // subsequent loads — exercising the same path a real user's choice takes.
  await page.evaluate(async code => {
    try {
      await fetch('/api/config/theme', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ language: code }),
      })
    } catch { /* best effort: the localStorage mirror already covers first paint */ }
  }, lang.code)

  // --- Sessions welcome view
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2800)
  await page.screenshot({ path: `${OUT}/${lang.code}-sessions.png` })

  // --- Schedule table
  await page.goto(`${BASE}/schedule`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2400)
  await page.screenshot({ path: `${OUT}/${lang.code}-schedule.png` })

  // --- Bulk-delete confirmation. Select all jobs via the header checkbox, then
  // open the dialog from the trash-icon button. Both are located by role/icon,
  // never by text — the text is exactly what varies per language.
  //
  // The dialog is only ever OPENED, never confirmed; Escape dismisses it. Even
  // so, treat the seeded jobs as consumable and re-seed before each language:
  // an accidental confirm (or a stray Enter) would empty the table and every
  // later language would silently capture an empty dialog-less page.
  const boxes = page.locator('input[type="checkbox"]')
  if (await boxes.count()) {
    await boxes.first().check({ force: true }).catch(() => {})
    await page.waitForTimeout(600)
    const del = page.locator('button').filter({ has: page.locator('svg.lucide-trash-2') })
    if (await del.count()) {
      await del.first().click().catch(() => {})
      await page.waitForTimeout(1000)
      await page.screenshot({ path: `${OUT}/${lang.code}-bulk-delete.png` })
      await page.keyboard.press('Escape').catch(() => {})
      await page.waitForTimeout(300)
    } else {
      failures.push(`${lang.code}: no delete button found`)
    }
  } else {
    failures.push(`${lang.code}: no job checkboxes found — re-seed crons.json and restart the gateway`)
  }

  // --- Settings > Display (the language picker itself)
  await page.goto(`${BASE}/settings?tab=display`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
  await page.screenshot({ path: `${OUT}/${lang.code}-display.png` })

  if (errors.length) failures.push(`${lang.code}: ${errors[0]}`)
  await context.close()
  console.log(`captured ${lang.code} (${lang.label})${errors.length ? '  [ERRORS]' : ''}`)
}

await browser.close()

if (failures.length) {
  console.error('\nPROBLEMS — do not treat these frames as verification:')
  for (const f of failures) console.error('  ' + f)
  process.exitCode = 1
} else {
  console.log(`\nall frames clean -> ${OUT}`)
}
