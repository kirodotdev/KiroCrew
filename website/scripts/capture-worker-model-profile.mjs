/**
 * Screenshot harness: the Worker model / Worker reasoning effort pair on the
 * crewmate Profile card (`CrewProfileSettings`).
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist` and answers every
 * `/api/**` call from fixtures through `stubDashboardApi`. No gateway, no auth.
 *
 * Frames:
 *   01-settings-same-as   the pair at rest: "Same as Kiro", hint names the model
 *   02-settings-auto      after picking Default model (Auto): PUT body asserted,
 *                         hint names the choice and says the effort row folded
 *   03-settings-dark      the same section, dark theme, a listed worker model
 *   04-settings-other-reasoning  a listed worker model that reasons: the effort
 *                         row shows with "No level set" and its note
 *
 * Usage: node scripts/capture-worker-model-profile.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'worker-model-shots')
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: `member-${name}`,
  model: '', reasoning_effort: '', approval_mode: 'trust', worker_model: '', worker_reasoning_effort: '', ...extra,
})
let kiro = member('kiro', { display_name: 'Kiro', model: 'claude-opus-4.6', reasoning_effort: 'high', last_active_ts: now - 60, last_message: 'All checks are green.' })
const atlas = member('atlas', { display_name: 'Atlas', last_active_ts: now - 1500, last_message: 'Backend is quiet.' })
// A bare ARRAY: `fetchAvailableModels` reads it as the live list and marks the
// picker degraded on anything else.
const MODELS = [{ name: 'auto', description: 'Default' }, { name: 'claude-opus-4.6', description: '' }, { name: 'claude-sonnet-4.6', description: '' }, { name: 'claude-haiku-4.5', description: '' }]
const STORAGE = { 'mc-lang': 'en', 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '1', 'mc-members-panel-open': '0' }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false
const check = (name, ok, detail = '') => { console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`); if (!ok) failed = true }

let lastPut = null
const extra = async (path, route) => {
  if (path === '/api/members') { await json(route, { members: [kiro, atlas], default_agent: 'kirocrew' }); return true }
  if (path === '/api/models') { await json(route, MODELS); return true }
  if (path.startsWith('/api/agents/resolved-model')) { await json(route, { model: 'claude-opus-4.6', reasoning_effort: 'high' }); return true }
  if (path === '/api/agents/kiro' && route.request().method() === 'PUT') {
    lastPut = JSON.parse(route.request().postData() || '{}')
    kiro = { ...kiro, ...lastPut }
    await json(route, { ok: true, name: 'kiro' })
    return true
  }
  if (path === '/api/agents') { await json(route, { agents: [], default_agent: 'kiro' }); return true }
  if (path === '/api/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
  const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
  if (thread) { const slug = decodeURIComponent(thread[1]); await json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false }); return true }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: '', member: '', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) { await json(route, { slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  if (path === '/api/crew-board') { await json(route, { conductor: 'member-kiro', items: [], generated_at: now }); return true }
  if (path === '/api/crons') { await json(route, { jobs: [] }); return true }
  if (path === '/api/cron-folders') { await json(route, []); return true }
  if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
  if (path === '/api/teams') { await json(route, { teams: [] }); return true }
  return false
}

async function openCard(theme) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 1100 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme, extra, slots: [{ key: 'member-kiro', title: 'Kiro', mode: 'member', agent: 'kiro' }], localStorageEntries: STORAGE })
  await page.goto(`${base}/members?member=kiro`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 30000 })
  await page.getByTestId('member-identity-pill').click()
  await page.getByTestId('crew-profile-card').waitFor({ state: 'visible', timeout: 20000 })
  // The card remembers its last tab per browser; the pair lives on Profile.
  await page.getByTestId('crew-profile-tabs').getByRole('tab', { name: 'Profile' }).click()
  const settings = page.getByTestId('crew-profile-settings')
  await settings.waitFor({ state: 'visible', timeout: 20000 })
  await settings.scrollIntoViewIfNeeded()
  await page.waitForTimeout(600)
  return { context, page, settings }
}

const workerSelect = (page) => page.getByRole('combobox', { name: 'Worker model' })

/** 01: at rest. */
{
  const { context, page, settings } = await openCard('light')
  const text = (await settings.innerText()).replace(/\s+/g, ' ')
  check('worker model reads Same as Kiro', text.includes('Same as Kiro'), text)
  check('hint names what that runs', text.includes('Workers are the extra chats Kiro opens to split up a task. They run on claude-opus-4.6, the same as Kiro.'), text)
  // Labels render uppercase through CSS, and innerText follows the CSS.
  check('worker effort is offered (opus reasons)', /worker reasoning effort/i.test(text))
  check('model list loaded (no degraded notice)', !text.includes("Couldn't load the model list"), text)
  await settings.screenshot({ path: join(OUT, '01-settings-same-as.png') })
  console.log('wrote', join(OUT, '01-settings-same-as.png'))

  // 02: pick Auto.
  await workerSelect(page).click()
  await page.getByRole('option', { name: 'Auto (cheaper model)', exact: true }).click()
  await page.waitForTimeout(800)
  check('PUT carries worker_model=auto', lastPut?.worker_model === 'auto', JSON.stringify(lastPut))
  const after = (await settings.innerText()).replace(/\s+/g, ' ')
  check('hint names the Auto choice against Kiro\'s own model', after.includes('Workers run on an automatically chosen model, usually cheaper than claude-opus-4.6.'), after)
  check('hint says why the effort row folded', after.includes("Reasoning effort doesn't apply to this model."), after)
  check('worker effort hides on Auto with none stored', !/worker reasoning effort/i.test(after), after)
  await page.mouse.move(5, 5)
  await page.waitForTimeout(300)
  await settings.screenshot({ path: join(OUT, '02-settings-auto.png') })
  console.log('wrote', join(OUT, '02-settings-auto.png'))
  await context.close()
}

/** 03: dark, a listed worker model with its own effort. */
{
  kiro = { ...kiro, worker_model: 'claude-haiku-4.5', worker_reasoning_effort: '' }
  const { context, page, settings } = await openCard('dark')
  const text = (await settings.innerText()).replace(/\s+/g, ' ')
  check('a stored worker model is shown', text.includes('claude-haiku-4.5'), text)
  check('hint names the stored model', text.includes("Workers run on claude-haiku-4.5 instead of claude-opus-4.6. Reasoning effort doesn't apply to this model."), text)
  await settings.screenshot({ path: join(OUT, '03-settings-dark.png') })
  console.log('wrote', join(OUT, '03-settings-dark.png'))
  await context.close()
}

/** 04: light, a listed worker model that REASONS: the effort row stays, its empty option
 *  reads "The model's own default" (not "Same level as Kiro": under another model the
 *  crewmate's own level does not carry over). */
{
  kiro = { ...kiro, worker_model: 'claude-sonnet-4.6', worker_reasoning_effort: '' }
  const { context, page, settings } = await openCard('light')
  const text = (await settings.innerText()).replace(/\s+/g, ' ')
  check('effort row shows under a reasoning worker model', /worker reasoning effort/i.test(text), text)
  check('its empty option reads no level set', text.includes('No level set'), text)
  check('and the note names the level that is not copied', text.includes("Workers use their own default, not Kiro's (High)."), text)
  check('and not the crewmate level', !text.includes('Same level as Kiro'), text)
  await settings.screenshot({ path: join(OUT, '04-settings-other-reasoning.png') })
  console.log('wrote', join(OUT, '04-settings-other-reasoning.png'))
  await context.close()
}

await browser.close()
srv.close()
console.log(failed ? 'FAILED' : 'ALL CHECKS OK')
process.exit(failed ? 1 : 0)
