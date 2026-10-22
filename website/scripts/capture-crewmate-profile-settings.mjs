/**
 * Screenshot harness for the crewmate Profile card's settings group (#22960):
 *
 *   - the Profile tab ends in ONE Settings row under Notes, reading the current
 *     picks; tapping it pushes the Settings page over the card: an iOS-style
 *     group where Permissions, Model, Worker model (and the effort rows when the
 *     model reasons) each read their value before a chevron, and the dashboard
 *     lock is a switch with its help as the group's caption;
 *   - tapping a row there pushes a single-choice page one level deeper (one back
 *     control naming the page under it, the row's label as the title), one
 *     option per row with its description, a check on the stored value;
 *   - a pick saves and the page stays open with the check moved.
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`): no gateway, no auth, no kiro-cli.
 *
 * Usage: node scripts/capture-crewmate-profile-settings.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'crewmate-profile-settings')
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const iso = (secsAgo) => new Date((now - secsAgo) * 1000).toISOString()
/** One crewmate pinned to a reasoning model, so every row of the group shows. */
const MEMBER = {
  name: 'kiro', slug: 'kiro', display_name: 'Kiro', bound: true, slot_key: 'member-kiro', running: true,
  kiro_agent: 'frontend', workspace: '/srv/repos/kiro', memory_store: 'member-kiro', memory_version: 2, memory_owner: 'kiro',
  model: 'claude-opus-5', reasoning_effort: 'high', approval_mode: 'trust', source: 'kirocrew',
  last_active_ts: now - 60, last_message: 'All 85 checks are green. Only human review is left.',
}
const AGENTS = [{
  name: 'kiro', display_name: 'Kiro', kiro_agent: 'frontend', workspace: MEMBER.workspace, memory_store: MEMBER.memory_store,
  model: MEMBER.model, source: 'kirocrew', approval_mode: 'trust',
  description: 'Frontend crewmate for the Kiro Crew dashboard. Owns the chat surface, the members page and the Glass design language. Reviews every UI PR from a new-user point of view before it ships, and never merges.',
}]
const SLOTS = [
  { key: 'member-kiro', title: 'Kiro', mode: 'member', created: iso(86400), last_ts: iso(60), running: true, project: MEMBER.workspace, agent: 'kiro' },
]
const MODELS = [
  { model_name: 'auto', description: 'Default' },
  { model_name: 'claude-opus-5', description: 'Opus 5' },
  { model_name: 'claude-sonnet-5', description: 'Sonnet 5' },
  { model_name: 'claude-haiku-4', description: 'Haiku 4' },
]
const STORAGE = { 'mc-lang': 'en', 'mc-crewmates-onboarded': '1', 'mc-crewmates-page-entered': '1', 'mc-nav': '1', 'mc-members-panel-open': '0' }

/** Every PUT the card makes, so the checks can read what was written. */
const writes = []
/** When set, the next permission PUT is refused as a stale write (409) naming
 *  this stored value, the way the server answers a compare-and-set miss. */
let conflictOnce = null

/** Nothing watches, nudges or teams this crewmate: the quiet answers. */
const QUIET = { '/api/autonudge': { enabled: true, loops: [] }, '/api/monitors': { enabled: true, monitors: [] }, '/api/teams': { teams: [] } }

const extra = async (path, route) => {
  if (path === '/api/members') { await json(route, { members: [MEMBER], default_agent: 'kirocrew' }); return true }
  if (path === '/api/agents') { await json(route, { agents: AGENTS, default_agent: 'kiro' }); return true }
  if (path === '/api/agents/kiro' && route.request().method() === 'PUT') {
    const body = JSON.parse(route.request().postData() || '{}')
    if (conflictOnce && 'approval_mode' in body) {
      const stored = conflictOnce
      conflictOnce = null
      MEMBER.approval_mode = stored
      await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ code: 'approval_mode_conflict', approval_mode: stored }) })
      return true
    }
    writes.push(body)
    // The record follows the write, so the roster re-read shows the new value.
    for (const k of ['approval_mode', 'model', 'reasoning_effort', 'worker_model', 'worker_reasoning_effort']) if (k in body) MEMBER[k] = body[k]
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/agents/resolved-model') { await json(route, { model: 'claude-opus-5', reasoning_effort: 'medium' }); return true }
  if (path === '/api/models') { await json(route, MODELS); return true }
  if (path === '/api/crons') { await json(route, { jobs: [] }); return true }
  if (path === '/api/cron-folders') { await json(route, []); return true }
  if (path === '/api/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
  if (/^\/api\/members\/[^/]+\/thread$/.test(path)) { await json(route, { slot_key: 'member-kiro', slug: 'kiro', member: 'kiro', created: false }); return true }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: '', member: '', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) { await json(route, { slug: '', member: '', supported: true, text: '# Briefing\n', updated_ts: now - 3600, redacted: false, truncated: false }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  if (/^\/api\/members\/[^/]+\/dashboard/.test(path)) { await json(route, { structure_locked: false }); return true }
  if (/^\/api\/members\/[^/]+\/goals$/.test(path)) { await json(route, { goals: [] }); return true }
  if (/^\/api\/chat\/slots\/member-kiro\/(model|reasoning-effort)$/.test(path)) { await json(route, { ok: true }); return true }
  if (/^\/api\/chat\/slots\/member-kiro$/.test(path)) { await json(route, { key: 'member-kiro', title: 'Kiro', running: true, messages: [] }); return true }
  if (path in QUIET) { await json(route, QUIET[path]); return true }
  if (/^\/api\/(autonudge|monitors)\/slot\//.test(path)) { await json(route, { enabled: true, loop: null, monitor: null }); return true }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function openProfile(theme, record = false) {
  const context = await browser.newContext({
    viewport: { width: 1500, height: 940 }, deviceScaleFactor: record ? 1 : 2, colorScheme: theme,
    ...(record ? { recordVideo: { dir: join(OUT, 'video-raw'), size: { width: 1500, height: 940 } } } : {}),
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme, extra, slots: SLOTS, localStorageEntries: STORAGE })
  await page.goto(`${base}/members?member=kiro`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(600)
  await page.getByTestId('member-identity-pill').click()
  const card = page.getByTestId('crew-profile-card')
  await card.waitFor({ state: 'visible', timeout: 10000 })
  await page.getByTestId('crew-profile-tabs').getByRole('tab', { name: 'Profile' }).click()
  await page.getByTestId('crew-profile-settings-row').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(700)
  await page.mouse.move(5, 5)
  return { context, page, card }
}
/** From the Profile tab, open the Settings page (the group). */
async function openSettings(page) {
  await page.getByTestId('crew-profile-settings-row').click()
  await page.getByTestId('crew-profile-settings').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(500)
}
/** A still with the pointer off the card: a hovered option would read as a second selection. */
async function shot(card, path) {
  const page = card.page()
  await page.mouse.move(5, 5)
  await page.waitForTimeout(150)
  await card.screenshot({ path })
}
const rowValue = (page, key) => page.getByTestId(`crew-profile-setting-${key}-value`).innerText()

/** Stills, both themes. */
for (const theme of ['light', 'dark']) {
  MEMBER.approval_mode = 'trust'; MEMBER.worker_model = ''; writes.length = 0
  const { context, page, card } = await openProfile(theme)

  // 00: the Profile tab holds one Settings row and nothing else of the settings.
  const srow = page.getByTestId('crew-profile-settings-row')
  check(`[${theme}] the card holds one Settings row reading the picks`, (await srow.innerText()).includes('Trust · claude-opus-5') && !(await srow.innerText()).includes('Same as'), await srow.innerText())
  check(`[${theme}] no group on the card`, (await page.getByTestId('crew-profile-settings').count()) === 0)
  await srow.scrollIntoViewIfNeeded()
  await page.waitForTimeout(200)
  await shot(card, join(OUT, `00-profile-tab-${theme}.png`))

  // 01: the Settings page: the group. No select; every row reads its value.
  await openSettings(page)
  const group = page.getByTestId('crew-profile-settings')
  check(`[${theme}] the Settings page is titled Settings, back names the crewmate`, (await page.getByTestId('crew-profile-pushed-title').innerText()).trim() === 'Settings' && (await page.getByTestId('crew-profile-back').innerText()).includes('Kiro'))
  check(`[${theme}] the page holds no form control`, (await group.locator('select, [role="combobox"], [role="listbox"]').count()) === 0)
  check(`[${theme}] Permissions reads Trust`, (await rowValue(page, 'approval_mode')) === 'Trust')
  check(`[${theme}] Model reads the pin`, (await rowValue(page, 'model')) === 'claude-opus-5')
  check(`[${theme}] Reasoning effort shows for a model that reasons`, (await rowValue(page, 'reasoning_effort')) === 'High')
  check(`[${theme}] Worker model reads Same as Kiro`, (await rowValue(page, 'worker_model')) === 'Same as Kiro')
  check(`[${theme}] the lock is a switch in the group`, (await group.getByRole('switch', { name: 'Lock dashboard layout' }).count()) === 1)
  await shot(card, join(OUT, `01-settings-page-${theme}.png`))

  // 02: Permissions → a page one level deeper: one back control naming the
  // Settings page, the row's title, three options with what each lets the
  // crewmate do, a check on Trust.
  await page.getByTestId('crew-profile-setting-approval_mode').click()
  const pg = page.getByTestId('crew-profile-setting-page-approval_mode')
  await pg.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(500)
  check(`[${theme}] the page is titled with the row`, (await page.getByTestId('crew-profile-pushed-title').innerText()).trim() === 'Permissions')
  check(`[${theme}] one back control, naming the page under it`, (await page.getByTestId('crew-profile-back').count()) === 1 && (await page.getByTestId('crew-profile-back').innerText()).includes('Settings'))
  check(`[${theme}] three options, Trust checked`, (await pg.getByRole('radio').count()) === 3 && (await pg.getByRole('radio', { checked: true }).innerText()).startsWith('Trust'))
  await shot(card, join(OUT, `02-permissions-page-${theme}.png`))

  // 03: pick Normal → one compare-and-set PUT; the page stays, check moved.
  await pg.getByRole('radio', { name: /Normal/ }).click()
  await page.waitForFunction(() => document.querySelector('[data-testid="crew-profile-setting-page-approval_mode"] [role="radio"][aria-checked="true"]')?.textContent?.startsWith('Normal'), null, { timeout: 10000 })
  await page.waitForTimeout(400)
  check(`[${theme}] the pick wrote one compare-and-set PUT`, writes.length === 1 && writes[0].approval_mode === 'normal' && writes[0].expected_approval_mode === 'trust', JSON.stringify(writes))
  check(`[${theme}] the page stays open after the pick`, await pg.isVisible())
  await shot(card, join(OUT, `03-permissions-picked-${theme}.png`))
  await page.getByTestId('crew-profile-back').click()
  await page.waitForTimeout(500)
  check(`[${theme}] the row behind reads the new value`, (await rowValue(page, 'approval_mode')) === 'Normal')

  // 04: Worker model → the page with the resolved-value caption under the list.
  await page.getByTestId('crew-profile-setting-worker_model').click()
  const wm = page.getByTestId('crew-profile-setting-page-worker_model')
  await wm.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(500)
  check(`[${theme}] the worker page names what the workers run on`, (await wm.innerText()).includes('They run on claude-opus-5, the same as Kiro.'))
  await shot(card, join(OUT, `04-worker-model-page-${theme}.png`))
  await page.getByTestId('crew-profile-back').click()
  await page.waitForTimeout(500)

  // 05-07: the other three pages, each with its caption under the list.
  for (const [n, key, caption] of [
    ['05', 'model', 'Using claude-opus-5'],
    ['06', 'reasoning_effort', 'Higher levels think longer, answer slower, and cost more.'],
    ['07', 'worker_reasoning_effort', 'Reasoning effort High, the same as Kiro.'],
  ]) {
    await page.getByTestId(`crew-profile-setting-${key}`).click()
    const pg2 = page.getByTestId(`crew-profile-setting-page-${key}`)
    await pg2.waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(500)
    const text = await pg2.innerText()
    // The Model page checks the pin; "Inherited" would show the resolve caption instead.
    check(`[${theme}] the ${key} page carries its caption`, key === 'model' ? (await pg2.getByRole('radio', { checked: true }).innerText()).includes('claude-opus-5') : text.includes(caption), text.slice(0, 80))
    await shot(card, join(OUT, `${n}-${key.replace(/_/g, '-')}-page-${theme}.png`))
    await page.getByTestId('crew-profile-back').click()
    await page.waitForTimeout(500)
  }

  // 08: a refused pick. The record moved elsewhere (409 approval_mode_conflict):
  // the page stays open, the check lands on what is stored, the notice sits on
  // the page where the person is looking.
  conflictOnce = 'trust_reads'
  await page.getByTestId('crew-profile-setting-approval_mode').click()
  await pg.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  await pg.getByRole('radio', { name: /Trust/ }).click()
  await pg.getByTestId('crew-profile-settings-notice').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  check(`[${theme}] a refused pick shows the stored value and the notice on the page`, (await pg.getByRole('radio', { checked: true }).innerText()).startsWith('Reads') && (await pg.getByTestId('crew-profile-settings-notice').innerText()).includes('changed elsewhere'))
  await shot(card, join(OUT, `08-permissions-refused-${theme}.png`))
  await context.close()
}

/** The walkthrough, recorded: row → Settings → Permissions → pick → back → Model → back → back. */
{
  MEMBER.approval_mode = 'trust'
  mkdirSync(join(OUT, 'video-raw'), { recursive: true })
  const { context, page } = await openProfile('light', true)
  const beat = (ms = 1200) => page.waitForTimeout(ms)
  await page.getByTestId('crew-profile-settings-row').scrollIntoViewIfNeeded()
  await beat(1500)
  await page.getByTestId('crew-profile-settings-row').click()
  await beat(1500)
  await page.getByTestId('crew-profile-setting-approval_mode').click()
  await beat(1500)
  await page.getByTestId('crew-profile-setting-page-approval_mode').getByRole('radio', { name: /Reads/ }).click()
  await beat(1500)
  await page.getByTestId('crew-profile-back').click()
  await beat(1200)
  await page.getByTestId('crew-profile-setting-model').click()
  await beat(1500)
  await page.getByTestId('crew-profile-back').click()
  await beat(1200)
  // Back once more: the Settings page pops and the card is bare again.
  await page.getByTestId('crew-profile-back').click()
  await beat(1200)
  await context.close()
  const raw = readdirSync(join(OUT, 'video-raw')).find((f) => f.endsWith('.webm'))
  if (raw) renameSync(join(OUT, 'video-raw', raw), join(OUT, 'walkthrough.webm'))
}

await browser.close()
srv.close()
console.log(failed ? 'SOME CHECKS FAILED' : 'all checks OK', OUT)
process.exit(failed ? 1 : 0)
