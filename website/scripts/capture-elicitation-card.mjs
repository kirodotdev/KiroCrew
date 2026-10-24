/**
 * Screenshots of the MCP elicitation card in its three states (a form ask, a URL
 * ask, an answered row), light and dark, from the isolated capture entry
 * website/capture/elicitation-card.html.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort   # in another shell
 *   node scripts/capture-elicitation-card.mjs http://127.0.0.1:6814 ../temp-screenshots/elicitation-card
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/elicitation-card'
mkdirSync(OUT, { recursive: true })

const SCENES = ['form', 'url', 'resolved', 'declined', 'cancelled']
const THEMES = ['dark', 'light']
/** Interaction states on the form scene: the required-field hint (Submit with
 *  the required box unchecked... a boolean is a valid false, so a required
 *  string field is what the hint fires on) and a refused answer (the gateway's
 *  400 rendered through ErrorNotice). */
const INTERACTIONS = ['hint', 'refused']

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
for (const scene of SCENES) {
  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: 700, height: 520 } })
    page.on('pageerror', e => { console.error(`  pageerror: ${e.message}`); failures++ })
    await page.goto(`${BASE}/capture/elicitation-card.html?scene=${scene}&theme=${theme}`, { waitUntil: 'networkidle' })
    const card = page.locator('[data-testid="elicitation-card"]')
    await card.waitFor()
    const buttons = await card.locator('button').count()
    const settled = scene === 'resolved' || scene === 'declined' || scene === 'cancelled'
    if (settled ? buttons !== 0 : buttons < 2) {
      console.error(`FAIL: scene=${scene} rendered ${buttons} button(s)`); failures++
    }
    const file = `${OUT}/elicitation-${scene}-${theme}.png`
    await page.locator('[data-capture-root]').screenshot({ path: file })
    console.log(`wrote ${file} (buttons=${buttons})`)
    await page.close()
  }
}
for (const state of INTERACTIONS) {
  for (const theme of THEMES) {
    const page = await browser.newPage({ viewport: { width: 700, height: 560 } })
    page.on('pageerror', e => { console.error(`  pageerror: ${e.message}`); failures++ })
    // The gateway's refusal of a value the schema rejects, as the route sends it.
    await page.route('**/api/chat/slots/*/elicitation', route => route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ error: "'Keep a backup for (days)' is above its maximum", code: 'invalid_content' }),
    }))
    await page.goto(`${BASE}/capture/elicitation-card.html?scene=form&theme=${theme}&required=reason`, { waitUntil: 'networkidle' })
    const card = page.locator('[data-testid="elicitation-card"]')
    await card.waitFor()
    if (state === 'refused') await page.getByLabel('Confirm deletion').check()
    if (state === 'refused') await page.getByLabel('Reason').fill('cleanup')
    await card.getByRole('button', { name: /submit/i }).click()
    const probe = state === 'hint' ? card.getByRole('status') : page.getByTestId('elicitation-answer-error')
    try {
      await probe.waitFor({ timeout: 5000 })
    } catch {
      console.error(`FAIL: state=${state} did not render its ${state === 'hint' ? 'hint' : 'ErrorNotice'}`); failures++
    }
    const file = `${OUT}/elicitation-form-${state}-${theme}.png`
    await page.locator('[data-capture-root]').screenshot({ path: file })
    console.log(`wrote ${file}`)
    await page.close()
  }
}
await browser.close()
process.exit(failures ? 1 : 0)
