/**
 * Screenshot runner for capture/error-card-model-unentitled.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort
 *   node scripts/capture-error-card-model-unentitled.mjs http://127.0.0.1:6831 <outdir>
 *
 * Captures the before/after sheet in both themes, element-scoped to the
 * capture root. Asserts the AFTER card really is the entitlement shape (both
 * action buttons present, no Continue) and the BEFORE card really is the
 * Continue shape, so a frame cannot photograph the wrong state.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/error-card-model-unentitled'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({
    viewport: { width: 820, height: 1400 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/error-card-model-unentitled.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    const before = page.locator('[data-episode="before"] [data-testid="error-card"]')
    const after = page.locator('[data-episode="after"] [data-testid="error-card"]')
    await before.waitFor({ timeout: 10000 })
    await after.waitFor({ timeout: 10000 })
    if (!(await page.locator('[data-episode="before"] [data-testid="error-card-continue"]').count())) {
      throw new Error('BEFORE episode lacks the Continue button')
    }
    if (await page.locator('[data-episode="after"] [data-testid="error-card-continue"]').count()) {
      throw new Error('AFTER episode unexpectedly offers Continue')
    }
    for (const id of ['error-card-pick-model', 'error-card-default-model', 'error-card-both-hint']) {
      if (!(await page.locator(`[data-episode="after"] [data-testid="${id}"]`).count())) {
        throw new Error(`AFTER episode lacks ${id}`)
      }
    }
    // The "do both" line quotes each button by its label, so the pair cannot
    // read as one action twice; a raw {{placeholder}} means interpolation broke.
    const bothHint = (await page.locator('[data-episode="after"] [data-testid="error-card-both-hint"]').innerText()) ?? ''
    for (const label of ['Choose a model for this session', 'Change default model']) {
      if (!bothHint.includes(label)) throw new Error(`AFTER both-hint does not name the button "${label}": ${bothHint}`)
    }
    if (bothHint.includes('{{')) throw new Error(`AFTER both-hint leaked an interpolation placeholder: ${bothHint}`)
    // Popout/embed: picker only, no settings action, no "do both" line.
    if (!(await page.locator('[data-episode="after-popout"] [data-testid="error-card-pick-model"]').count())) {
      throw new Error('POPOUT episode lacks the picker action')
    }
    for (const id of ['error-card-default-model', 'error-card-both-hint', 'error-card-continue']) {
      if (await page.locator(`[data-episode="after-popout"] [data-testid="${id}"]`).count()) {
        throw new Error(`POPOUT episode unexpectedly has ${id}`)
      }
    }
    // Pane: prose only, plus the "where those live" line.
    if (await page.locator('[data-episode="after-pane"] button').count()) {
      throw new Error('PANE episode unexpectedly has a button')
    }
    for (const ep of ['after-pane', 'after-popout']) {
      if (!(await page.locator(`[data-episode="${ep}"] [data-testid="error-card-elsewhere-hint"]`).count())) {
        throw new Error(`${ep} episode lacks the elsewhere hint`)
      }
    }
    // Popout/embed has a live picker button: its hint must name only Settings.
    const popoutHint = await page.locator('[data-episode="after-popout"] [data-testid="error-card-elsewhere-hint"]').textContent()
    if (/picker/i.test(popoutHint || '')) throw new Error('POPOUT hint wrongly says the picker is elsewhere')
    if (await page.locator('[data-episode="after"] [data-testid="error-card-elsewhere-hint"]').count()) {
      throw new Error('dashboard AFTER episode unexpectedly has the elsewhere hint')
    }
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({
      path: `${OUT}/error-card-model-unentitled-${theme}.png`,
    })
    console.log(`${theme}: OK`)
  } catch (e) {
    console.error(`${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
