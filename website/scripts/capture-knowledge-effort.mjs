/**
 * Capture harness for the Knowledge tab's LLM-pool controls.
 *
 * Runs the REAL built SPA (website/dist) behind a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent (same
 * pattern as capture-agent-backend-probe.mjs).
 *
 * Scenes (Knowledge Library > Settings tab — the single home for these knobs):
 *   1. DEFAULT state: '' effort = Default, model 'auto' (run-time hint), pool
 *      size 3, restart badges on effort + pool-size rows.
 *   2. PICKED state: extraction effort 'low', model claude-fable-5
 *      (effort-capable), pool size 5.
 *   3. LEVELS state: the effort selector OPEN, showing every level
 *      (Default/Low/Medium/High/Extra High/Max).
 *   4. UNSUPPORTED state: model claude-haiku-4.5 (no effort support) — the
 *      effort select is DISABLED and the row hint says why.
 *
 * Usage: node scripts/capture-knowledge-effort.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/knowledge-effort'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** The knowledge section of GET /api/config/kirocrew. */
let knowledge = {}

const { srv, base } = await serveDist()

const browser = await chromium.launch({
  // playwright 1.58 pins chromium_headless_shell-1208, which is not in the
  // local cache; point CHROME_PATH at a cached headless shell to reuse it.
  executablePath: process.env.CHROME_PATH || undefined,
  headless: true,
})
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()

const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })

await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/config/kirocrew') {
    // PATCH: apply the dotted key into the fixture so the refetch shows the
    // picked values (the real handler's semantics).
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() || {}
      const key = typeof body.path === 'string' ? body.path : ''
      if (key.startsWith('knowledge.')) {
        knowledge = { ...knowledge, [key.slice('knowledge.'.length)]: body.value }
      }
      return json(route, {})
    }
    return json(route, {
      agent: { model: 'auto', reasoning_effort: '', role_models: {}, role_efforts: {} },
      dashboard: {},
      knowledge: { auto_ingest_chunk_budget: 150, max_sources: 50, embed_rate_limit: 120, extraction_pool_size: 3, ...knowledge },
    })
  }
  if (path === '/api/models') {
    // Must be a BARE ARRAY with `model_name` rows: the ACP adapter rejects
    // non-array bodies as degraded (acp.ts fetchAvailableModels) and would
    // serve an auto-only fallback, so the haiku option never renders.
    return json(route, [
      { model_name: 'auto', description: 'Let Kiro choose' },
      { model_name: 'claude-haiku-4.5', description: 'Fastest' },
      { model_name: 'claude-fable-5', description: 'Fable' },
    ])
  }
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

/** Open Knowledge Library > Settings and wait for the pool rows. */
const openKnowledgeSettings = async () => {
  await page.goto(`${base}/capabilities?tab=knowledge`, { waitUntil: 'domcontentloaded' })
  // Settle like capture-default-model.mjs does: the SPA keeps animating the tab
  // rail on cold start, and a click before it stabilizes times out.
  await page.waitForTimeout(2600)
  // The Knowledge page's tab rail is internal state; click through to Settings.
  // Scoped to `.last()`: the left nav's own "Settings" button matches otherwise.
  await page.getByRole('button', { name: 'Settings', exact: true }).last().click()
  await page.getByText('Ingestion Settings', { exact: true }).waitFor({ timeout: 20000 })
  // Label casing must match the i18n strings (pages.knowledge.settings.*).
  await page.getByText('Extraction effort', { exact: true }).waitFor({ timeout: 20000 })
}

/** Scroll the pool rows into view so the shot shows all the controls. */
const scrollRowsIntoView = async () => {
  await page.getByText('Extraction effort', { exact: true }).first()
    .evaluate(el => el.closest('[data-setting-key], div')?.scrollIntoView({ block: 'center' }))
  await page.waitForTimeout(400)
}

// ── Scene 1: default state ──
await openKnowledgeSettings()
await scrollRowsIntoView()
await page.screenshot({ path: `${OUT}/knowledge-effort-default.png` })
console.log('wrote scene 1')

// ── Scene 2: picked values ──
await page.getByRole('combobox', { name: 'Extraction model' }).click()
await page.waitForTimeout(700)
// An effort-capable model: the effort select disables on haiku.
await page.getByRole('option', { name: 'claude-fable-5' }).click()
await page.getByRole('combobox', { name: 'Extraction effort' }).click()
await page.waitForTimeout(700)
await page.getByRole('option', { name: 'Low' }).click()
const poolInput = page.getByRole('spinbutton', { name: 'Extraction pool size' })
await poolInput.fill('5')
await poolInput.blur()
await page.waitForTimeout(800)
await scrollRowsIntoView()
await page.screenshot({ path: `${OUT}/knowledge-effort-picked.png` })
console.log('wrote scene 2')

// ── Scene 3: effort selector open, every level visible ──
await page.getByRole('combobox', { name: 'Extraction effort' }).click()
await page.waitForTimeout(700)
await page.screenshot({ path: `${OUT}/knowledge-effort-levels.png` })
console.log('wrote scene 3')
// Leave the value as picked: dismiss the dropdown without choosing.
await page.keyboard.press('Escape')
await page.waitForTimeout(400)

// ── Scene 4: effort-incapable model — select disabled with the reason ──
await page.getByRole('combobox', { name: 'Extraction model' }).click()
await page.waitForTimeout(700)
await page.getByRole('option', { name: 'claude-haiku-4.5' }).click()
// The row hint flips to the reasoning-model explanation once the effective
// model is known and cannot take effort; the select renders disabled.
await page
  .getByText('Applies only on reasoning-capable models', { exact: false })
  .waitFor({ timeout: 20000 })
await page.waitForTimeout(800)
await scrollRowsIntoView()
await page.screenshot({ path: `${OUT}/knowledge-effort-unsupported.png` })
console.log('wrote scene 4')

if (errors.length) {
  console.error('PAGE ERRORS:\n' + errors.join('\n'))
}
await browser.close()
await srv.close()
console.log('done')
