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
 *   <code>-picker.png        the open language dropdown (Auto row asserted, not
 *                            merely photographed — see the check below)
 *
 * Usage:
 *   ./dev-backend.sh &                       # real gateway on :6777
 *   TOKEN=$(curl -s -H "X-Local-Secret: $(cat .kirocrew-dev/.local_secret)" \
 *     http://127.0.0.1:6777/api/token/local | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
 *   node scripts/capture-i18n-languages.mjs <outDir> <baseUrl> "$TOKEN"
 *
 * Mint the token FRESH for each run. A stale/expired one still returns HTTP 200
 * for `/` (the SPA shell loads) but leaves every authenticated surface empty, so
 * the failure looks like "no job checkboxes found" for every language rather than
 * "auth expired". That is the harness working — it refuses to call unverified
 * frames evidence — but the message points at the wrong cause.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

const OUT = process.argv[2] || '/tmp/i18n-shots'
const BASE = (process.argv[3] || 'http://127.0.0.1:6777').replace(/\/$/, '')
const TOKEN = process.argv[4] || ''
mkdirSync(OUT, { recursive: true })

/**
 * Languages to capture, with the endonym the picker must show — read from the
 * SAME source of truth the app ships (`src/i18n/languages.ts`) rather than a
 * hand-kept copy, so a newly shipped language is captured automatically instead
 * of being silently skipped by a stale list. Parsed rather than imported because
 * this script is plain node with no TS loader.
 */
const LANGUAGES = [...readFileSync(
  new URL('../src/i18n/languages.ts', import.meta.url), 'utf-8',
).matchAll(/\{\s*code:\s*'([^']+)',\s*label:\s*'([^']+)'\s*\}/g)]
  .map(([, code, label]) => ({ code, label }))

if (!LANGUAGES.length) {
  console.error('Could not parse SUPPORTED_LANGUAGES from src/i18n/languages.ts — '
    + 'fix the parse rather than hard-coding a list, or the next language ships uncaptured.')
  process.exit(2)
}
console.log(`capturing ${LANGUAGES.length} languages: ${LANGUAGES.map(l => l.code).join(', ')}`)

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

  // --- Language dropdown, and ASSERT the Auto row rather than only picturing it.
  // The dropdown auto-scrolls to the current selection, so the Auto row is often
  // out of frame — a screenshot alone cannot verify it. Reading the option text
  // is what caught the row rendering from the wrong catalog ("自动 — Deutsch")
  // when the harness seeded localStorage but not the server value.
  const combo = page.getByRole('combobox').first()
  if (await combo.count()) {
    await combo.click().catch(() => {})
    await page.waitForTimeout(800)
    const options = await page.getByRole('option').allInnerTexts()
    const auto = options[0] ?? ''
    // Auto must name THIS language's resolved endonym, and must not have
    // regressed to the old "(follow browser)" parenthetical (wrong in the app,
    // which follows the OS rather than a browser).
    if (!auto.includes('—')) failures.push(`${lang.code}: Auto row missing its resolved language: ${JSON.stringify(auto)}`)
    if (/\(/.test(auto)) failures.push(`${lang.code}: Auto row regained a parenthetical: ${JSON.stringify(auto)}`)
    const missing = LANGUAGES.filter(l => !options.some(o => o.includes(l.label)))
    if (missing.length) failures.push(`${lang.code}: picker is missing ${missing.map(m => m.label).join(', ')}`)
    await page.evaluate(() => document.querySelector('[role="option"]')?.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${lang.code}-picker.png` })
    await page.keyboard.press('Escape').catch(() => {})
    console.log(`  ${lang.code} Auto row: ${JSON.stringify(auto)}`)
  } else {
    failures.push(`${lang.code}: no language combobox found`)
  }

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
