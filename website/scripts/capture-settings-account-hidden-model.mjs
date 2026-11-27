/**
 * Capture harness for a model the Kiro account hid, in Settings -> Chat ->
 * Selectable Models.
 *
 * Runs the REAL built SPA (website/dist) behind a static file server with every
 * /api/** call answered from fixtures -- no gateway, no token, no agent.
 *
 * `/api/models` is the KAS bundle route's answer: the account's whole served
 * list, one row carrying `hidden_by_account: true`. The list is opened so the
 * frame shows that row locked and unticked, its description line opening with
 * the reason and the link to app.kiro.dev, among ordinary rows (one of them
 * hidden by Crew's own list, so the two kinds sit side by side). The summary
 * on the closed trigger counts the account-hidden row in the total, never in
 * the selection.
 *
 * The row is also READ BACK: the checkbox must be disabled and unchecked, the
 * reason line must be inside the row, and the link must point at the Kiro
 * model settings page.
 *
 * Usage: node scripts/capture-settings-account-hidden-model.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/settings-account-hidden-model'
const PROJECT = '/home/user/workspace/KiroCrew'
const KIRO_MODEL_SETTINGS_URL = 'https://app.kiro.dev/settings/model'

mkdirSync(OUT, { recursive: true })

const models = [
  { model_name: 'auto', description: 'Models chosen by task for optimal usage and consistent quality', rate_multiplier: 1.0, context_window: 1000000 },
  { model_name: 'claude-opus-4.7', description: 'Claude Opus 4.7 model with 1M context window', rate_multiplier: 2.2, context_window: 1000000 },
  { model_name: 'claude-sonnet-4.6', description: 'Claude Sonnet 4.6 model with 1M context window', rate_multiplier: 1.0, context_window: 1000000 },
  { model_name: 'claude-haiku-4.5', description: 'The latest Claude Haiku model', rate_multiplier: 0.4, context_window: 200000, hidden_by_account: true },
  { model_name: 'glm-5', description: 'GLM-5 model', rate_multiplier: 0.5, context_window: 200000 },
  { model_name: 'gemini-2.5-pro', description: 'Gemini 2.5 Pro model', rate_multiplier: 1.0, context_window: 1000000 },
]

const mcConfig = {
  agent: { bot_name: 'Kiro', model: 'auto', reasoning_effort: '', role_models: {}, role_efforts: {} },
  session: { autocompact_pct: 70.0 },
  dashboard: { user_role: '', user_role_other: '', user_technical_level: '', verbosity: 'normal', widget_density: 'comfortable', language: 'en' },
}
const dashboardConfig = {
  model_picker_hidden_models: ['glm-5'],
  model_picker_configured: true,
  verbosity: 'default',
  default_memory_mode: 'persistent',
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const fixedApi = makeFixedApi(PROJECT)
let failed = false

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 980 } })
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/models') return json(route, models)
    if (path === '/api/dashboard/config') {
      if (route.request().method() !== 'GET') return json(route, { ok: true })
      return json(route, dashboardConfig)
    }
    if (path === '/api/config/kirocrew') {
      if (route.request().method() === 'PATCH') return json(route, { ok: true })
      return json(route, mcConfig)
    }
    if (path === '/api/tips/state') return json(route, { enabled_config: true, opted_out: false })
    return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi })
  })
  await page.addInitScript((mode) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', mode)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-lang', 'en')
  }, theme)

  await page.goto(`${base}/settings?tab=chat&sub=models`, { waitUntil: 'domcontentloaded' })
  const trigger = page.getByRole('button', { name: 'Selectable Models' })
  await trigger.waitFor({ timeout: 30000 })
  await trigger.scrollIntoViewIfNeeded()
  await page.waitForTimeout(500)
  const summary = (await trigger.innerText()).replace(/\s+/g, ' ').trim()
  await page.screenshot({ path: `${OUT}/${theme}-closed.png` })

  await trigger.click()
  const haiku = page.getByRole('checkbox', { name: 'claude-haiku-4.5' })
  await haiku.waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${theme}-open.png` })

  const row = haiku.locator('xpath=ancestor::label[1]')
  const reason = row.locator('[data-multi-select-locked-reason]')
  const reasonText = (await reason.innerText()).replace(/\s+/g, ' ').trim()
  const href = await reason.locator('a').getAttribute('href')
  const ok =
    (await haiku.isDisabled()) &&
    !(await haiku.isChecked()) &&
    reasonText.startsWith('Hidden through your app.kiro.dev settings') &&
    href === KIRO_MODEL_SETTINGS_URL &&
    summary.includes('4 / 6')
  console.log(`${theme}: ${ok ? 'OK' : 'MISMATCH'} summary="${summary}" row="${reasonText}" href=${href}`)
  if (!ok) failed = true
  await page.close()
}

await browser.close()
srv.close()
process.exit(failed ? 1 : 0)
