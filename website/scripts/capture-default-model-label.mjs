/**
 * Capture harness for the renamed model row in Settings -> Chat.
 *
 * Runs the REAL built SPA (website/dist) behind a static file server with every
 * /api/** call answered from fixtures -- no gateway, no token, no agent.
 *
 * Evidence for every string this change renames. On the chat row:
 *   1. the row's label ("Fallback Model" -> "Default Model");
 *   2. its hint, which no longer opens with "Fallback only:" while still saying
 *      that agents pinning their own model ignore the row;
 *   3. the composer picker's link out to it ("Global fallback for new
 *      sessions..." -> "Global default for new sessions...").
 *
 * And on the per-role rows, which claimed an inheritance from the chat rows that
 * `RoleModels.resolve_model` / `resolve_effort` never perform:
 *   4. the 'auto' option, which shared the chat row's "Default (auto)" and now
 *      reads "Auto (provider picks)";
 *   5. the role model hint, which named that option by its old label;
 *   6. the role effort hint, which said empty "inherits the chat default
 *      effort" -- something resolve_effort() does not do.
 *
 * The label is also PRINTED, not just photographed: the row is a Radix Select
 * whose accessible name comes from the visible caption, so reading it back is a
 * machine-checkable assertion that the rename reached the rendered accessible
 * name and not only the pixels. The hint lives behind a click-to-open InfoTip
 * rather than inline, so it needs a second frame with the tip open -- counting
 * it on the closed panel reports a false absence.
 *
 * Which label appears is a property of the BUILT bundle's locale catalog, so
 * the before/after pair is produced by building each revision and running this
 * twice -- there is no fixture that can flip it, and faking one would photograph
 * something no user can reach. The row is addressed through `data-setting-label`
 * for BOTH spellings, so the same script runs unchanged on either side.
 *
 * Usage: node scripts/capture-default-model-label.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/default-model-label'
const PREFIX = process.argv[3] || 'after'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

// `agent.model` is the value the row under test writes. Every other field
// carries an explicit value so the scene leans on no component-side default.
const mcConfig = {
  agent: {
    bot_name: 'Kiro',
    model: 'opus-4.8-1m',
    reasoning_effort: '',
    role_models: { background: 'haiku-4.5', subagent: 'auto' },
    role_efforts: { background: '', subagent: '' },
  },
  session: { autocompact_pct: 70.0 },
  dashboard: {
    user_role: '',
    user_role_other: '',
    user_technical_level: '',
    verbosity: 'normal',
    widget_density: 'comfortable',
    language: 'en',
  },
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 980 } })
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

// Pin theme and locale: without this the SPA negotiates a language from the
// runner's environment and the frame comes out in whichever one it picked.
await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-lang', 'en')
})

// Both spellings, so the anchor does not change with the thing under test.
const SPELLINGS = ['Default Model', 'Fallback Model']
const row = () =>
  page.locator(SPELLINGS.map((s) => `[data-setting-label="${s}"]`).join(', '))

await page.goto(`${base}/settings?tab=chat`, { waitUntil: 'domcontentloaded' })
await row().waitFor({ timeout: 30000 })
await row().scrollIntoViewIfNeeded()
await page.waitForTimeout(700)
await page.screenshot({ path: `${OUT}/${PREFIX}-settings-chat-model.png` })

// Read the row back by its accessible name. Whichever spelling this build
// carries is the one a screen reader announces, so resolve BOTH candidates
// instead of asserting one and crashing on the other side of the pair.
for (const name of SPELLINGS) {
  console.log(`${PREFIX}: combobox named ${JSON.stringify(name)} -> ${await page.getByRole('combobox', { name }).count()}`)
}

// The hint is a click-to-open tooltip scoped to this row, portal-rendered.
// Counting it on the closed panel reports a false absence.
const openHint = async (label, frame) => {
  const scope = page.locator(`[data-setting-label="${label}"]`)
  await scope.scrollIntoViewIfNeeded()
  await scope.getByRole('button', { name: 'More information' }).click()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-${frame}.png` })
  const body = await page.locator('body').innerText()
  await page.keyboard.press('Escape')
  await page.waitForTimeout(200)
  return body
}

const chatHint = await openHint(
  (await page.locator('[data-setting-label="Default Model"]').count()) ? 'Default Model' : 'Fallback Model',
  'settings-chat-model-hint',
)
console.log(`${PREFIX}: chat hint keeps the precedence fact -> ${/configured model ignore this setting/i.test(chatHint)}`)
console.log(`${PREFIX}: chat hint opens with "Fallback only:" -> ${/Fallback only:/.test(chatHint)}`)

// ── Per-role rows ──
// The option list is where the shared-label problem was visible: a role on auto
// showing the chat row's "Default (auto)" read as "inherit the row above".
const roleTrigger = () => page.getByRole('combobox', { name: 'Background Model' })
await roleTrigger().scrollIntoViewIfNeeded()
await roleTrigger().click()
await page.getByRole('option').first().waitFor({ timeout: 15000 })
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/${PREFIX}-role-model-options.png` })
const roleOptions = await page.getByRole('option').evaluateAll((els) => els.map((e) => e.textContent.trim()))
console.log(`${PREFIX}: role model options -> ${JSON.stringify(roleOptions)}`)
await page.keyboard.press('Escape')
await page.waitForTimeout(200)

const roleModelHint = await openHint('Background Model', 'role-model-hint')
console.log(`${PREFIX}: role model hint names 'Auto' -> ${/'Auto' lets the provider pick/.test(roleModelHint)}`)
console.log(`${PREFIX}: role model hint names 'Default' -> ${/'Default' lets the provider pick/.test(roleModelHint)}`)

const roleEffortHint = await openHint('Background Effort', 'role-effort-hint')
console.log(`${PREFIX}: role effort hint claims chat-effort inheritance -> ${/empty inherits the chat default effort/i.test(roleEffortHint)}`)
console.log(`${PREFIX}: role effort hint denies inheritance -> ${/does not inherit your Default Reasoning Effort/i.test(roleEffortHint)}`)

await browser.close()
srv.close()
console.log(`wrote screenshots to ${OUT}`)
