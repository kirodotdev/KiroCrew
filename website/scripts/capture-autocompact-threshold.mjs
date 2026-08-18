/**
 * Capture harness for the Auto-Compact Threshold control in Settings -> Chat.
 *
 * Runs the REAL built SPA (website/dist) behind a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent.
 *
 * Evidence for two things the diff changes:
 *   1. the option list now offers 70, which it did not before, so a session on
 *      the new default can render its own configured value;
 *   2. the "(default)" marker sits on 70 rather than on 90.
 *
 * Two scenes, driven by mutating the single config fixture the panel reads:
 *   - a new install on the shipped default (70)
 *   - an existing install that still stores 90, which this change deliberately
 *     leaves alone; the select shows 90 and no longer calls it the default
 *
 * Usage: node scripts/capture-autocompact-threshold.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/autocompact-threshold'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

// The one mutable fixture. `session.autocompact_pct` is the value under test;
// every other field carries an explicit value so no scene leans on a
// component-side default.
const BASE_CONFIG = {
  session: { autocompact_pct: 70.0 },
  dashboard: {
    user_role: '',
    user_role_other: '',
    user_technical_level: '',
    verbosity: 'normal',
    widget_density: 'comfortable',
    language: 'en',
  },
  agent: { bot_name: 'Kiro' },
}
let mcConfig = structuredClone(BASE_CONFIG)
const scene = (pct) => {
  mcConfig = structuredClone(BASE_CONFIG)
  mcConfig.session.autocompact_pct = pct
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
const fixedApi = makeFixedApi(PROJECT)

await page.route('**/api/**', (route) => {
  const path = new URL(route.request().url()).pathname

  if (path === '/api/config/kirocrew') {
    if (route.request().method() === 'PATCH') return json(route, { ok: true })
    return json(route, mcConfig)
  }
  if (path === '/api/tips/state') return json(route, { enabled_config: true, opted_out: false })

  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

const control = () => page.getByText('Auto-Compact Threshold', { exact: false })

/** Bring the Context card into view, let it settle, then shoot. */
const shoot = async (name) => {
  await control().waitFor({ timeout: 30000 })
  await control().scrollIntoViewIfNeeded()
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/${name}` })
}

// Scene 1 — a new install on the shipped default. The select reads 70 and the
// option list contains it; before this change 70 was absent entirely, so this
// value had no option to bind to.
scene(70.0)
await page.goto(`${base}/settings?tab=chat`, { waitUntil: 'domcontentloaded' })
await shoot('autocompact-default-70-dark.png')

// The control is a Radix Select: a trigger button plus a popover listbox, not a
// native <select>. Address the trigger by the aria-label SettingsSelect sets
// from the visible caption.
const trigger = () => page.getByRole('combobox', { name: 'Auto-Compact Threshold' })

/** Read the trigger's rendered text — this is the value a user actually sees. */
const triggerText = async () => (await trigger().innerText()).trim()

console.log('trigger text on the 70 fixture:', await triggerText())

// The open dropdown, which is where the moved "(default)" marker is legible.
await trigger().scrollIntoViewIfNeeded()
await trigger().click()
await page.getByRole('option').first().waitFor({ timeout: 15000 })
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/autocompact-options-dark.png` })

// Print the rendered option set as machine-checkable evidence that 70 exists
// and that the default marker moved off 90.
const options = await page.getByRole('option').evaluateAll((els) =>
  els.map((e) => e.textContent.trim()),
)
console.log('rendered options:', JSON.stringify(options))
await page.keyboard.press('Escape')
await page.waitForTimeout(200)

// Scene 2 — an existing install still storing 90. This change does not migrate
// it, so the control must show 90 and must not label it the default.
scene(90.0)
await page.reload({ waitUntil: 'domcontentloaded' })
await shoot('autocompact-existing-90-dark.png')
console.log('trigger text on the 90 fixture:', await triggerText())

// Light-theme variant of the default scene.
scene(70.0)
await page.reload({ waitUntil: 'domcontentloaded' })
await control().waitFor({ timeout: 30000 })
await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
await control().scrollIntoViewIfNeeded()
await page.waitForTimeout(500)
await page.screenshot({ path: `${OUT}/autocompact-default-70-light.png` })

await browser.close()
srv.close()
console.log(`wrote screenshots to ${OUT}`)
