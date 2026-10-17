/**
 * Screenshots of the Knowledge → Settings (Ingestion Settings) tab.
 *
 * Drives the ISOLATED capture entry (website/capture/knowledge-ingestion-settings.html),
 * which mounts the REAL SettingsTab against the real stylesheet, theme tokens and live
 * i18n catalog with a config snapshot seeded into the tab's own query key.
 *
 * Why not the full SPA: /knowledge?tab=settings needs a live gateway and a dashboard
 * credential; without one the shell renders its prerequisite gate and the frame would
 * document the wrong screen.
 *
 * The run ASSERTS the row set before writing a frame, so it can never quietly emit a
 * screenshot where a retired knob is still rendered — or where a surviving one has
 * gone missing. `--expect before` asserts the pre-removal row set instead, which is
 * how the before/after pair is shot from one harness.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6810 --strictPort   # in another shell
 *   node scripts/capture-knowledge-ingestion-settings.mjs http://127.0.0.1:6810 \
 *        ../temp-screenshots/knowledge-settings [--expect after|before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6810'
const OUT = process.argv[3] || '../temp-screenshots/knowledge-settings'
const expectArg = process.argv.indexOf('--expect')
const EXPECT = expectArg > -1 ? process.argv[expectArg + 1] : 'after'
if (!['after', 'before'].includes(EXPECT)) {
  throw new Error(`--expect must be after|before, got ${EXPECT}`)
}
mkdirSync(OUT, { recursive: true })

/** Rows that must render, in document order. */
const SURVIVING_ROWS = [
  'Auto-add documents',
  'Auto-add saved artifacts',
  'Embedding rate limit',
  'Extraction model',
  'Extraction pool size',
]

/** Rows the folder auto-registration removal takes away. */
const RETIRED_ROWS = [
  'Auto-register project documents',
  'Per-source chunk limit',
  'Max sources',
]

/**
 * Number inputs are the load-bearing count: two of the three retired rows were
 * number inputs, so the count distinguishes the two builds even if a label were
 * reworded. Before: chunk limit + max sources + embed rate + pool size. After:
 * embed rate + pool size.
 */
const EXPECTED_NUMBER_INPUTS = EXPECT === 'before' ? 4 : 2

const run = async () => {
  const browser = await chromium.launch(
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : undefined,
  )
  let failed = 0
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 900, height: 720 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    const page = await ctx.newPage()
    const errors = []
    page.on('pageerror', e => errors.push(e.message))
    await page.goto(`${BASE}/capture/knowledge-ingestion-settings.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    try {
      await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
      await page.getByText('Ingestion Settings').waitFor({ timeout: 10000 })
    } catch {
      console.error(
        `  FAIL ${theme}: the tab never rendered` + (errors.length ? ` (${errors[0]})` : ''),
      )
      failed += 1
      await ctx.close()
      continue
    }

    const labels = await page.$$eval('[data-capture-root] *', els =>
      els
        .filter(e => e.children.length === 0 && (e.textContent || '').trim())
        .map(e => (e.textContent || '').trim()),
    )
    const missing = SURVIVING_ROWS.filter(r => !labels.includes(r))
    const present = (EXPECT === 'before' ? [] : RETIRED_ROWS).filter(r => labels.includes(r))
    const absentBefore = (EXPECT === 'before' ? RETIRED_ROWS : []).filter(
      r => !labels.includes(r),
    )
    const numberInputs = await page.$$eval(
      '[data-capture-root] input[type="number"]',
      els => els.length,
    )

    if (missing.length || present.length || absentBefore.length ||
        numberInputs !== EXPECTED_NUMBER_INPUTS) {
      if (missing.length) console.error(`  FAIL ${theme}: missing row(s): ${missing.join(', ')}`)
      if (present.length) {
        console.error(`  FAIL ${theme}: retired row(s) still rendered: ${present.join(', ')}`)
      }
      if (absentBefore.length) {
        console.error(
          `  FAIL ${theme}: --expect before but row(s) absent: ${absentBefore.join(', ')}`,
        )
      }
      if (numberInputs !== EXPECTED_NUMBER_INPUTS) {
        console.error(
          `  FAIL ${theme}: expected ${EXPECTED_NUMBER_INPUTS} number input(s), saw ${numberInputs}`,
        )
      }
      failed += 1
      await ctx.close()
      continue
    }

    const target = await page.$('[data-capture-root]')
    await target.screenshot({ path: `${OUT}/${theme}-${EXPECT}.png` })
    console.log(
      `  ${theme}/${EXPECT} -> ${SURVIVING_ROWS.length} surviving row(s), ` +
        `${numberInputs} number input(s)`,
    )
    await ctx.close()
  }
  await browser.close()
  if (failed) {
    console.error(`${failed} frame(s) failed`)
    process.exit(1)
  }
}

run()
