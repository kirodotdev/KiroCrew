/**
 * Screenshot harness for RFC-step-1 PR #21463 (kirodotdev/KiroCrew):
 * the mate-conductor sourcing move is NOT invisible on a FRESH install.
 *
 * On a fresh install the first crewmate's `kiro_agent` becomes
 * `kirocrew-mate-conductor` instead of `kirocrew`, and that value is RENDERED
 * on four surfaces:
 *   1. the crew roster CARD — the "Built from" binding
 *   2. the crew roster TABLE — the "Built from" column cell
 *   3. the Crewmate SWITCHER — the idle row subtitle
 *   4. the New Crewmate DIALOG — the "Built from" default option + hint
 *      (after-only: the dialog default is the current frontend constant)
 *
 * This proves the change honestly: BEFORE (existing install, kiro_agent
 * "kirocrew") and AFTER (fresh install, "kirocrew-mate-conductor"), in light
 * and dark. Runs the REAL built SPA (website/dist) behind `serveDist`, answers
 * every /api/** from fixtures via `stubDashboardApi` — gateway-free, no
 * kiro-cli, no dashboard auth.
 *
 * Assert-then-shoot: every frame asserts the target DOM text (and scrolls the
 * element into view) BEFORE the PNG is written, so a stale or half-rendered
 * frame fails the run instead of shipping a dishonest image.
 *
 * Usage: node scripts/capture-mate-conductor-builtfrom.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || 'temp-screenshots/mate-conductor-builtfrom'
mkdirSync(OUT, { recursive: true })

const BEFORE = 'kirocrew' // existing install: the first crewmate reads off disk
const AFTER = 'kirocrew-mate-conductor' // fresh install: sourced in code

/** The roster the agents page shows. One crewmate (the first-run "default"),
 *  whose `kiro_agent` is the value this PR changes on a fresh install. */
function crews(builtFrom) {
  return [
    { name: 'default', kiro_agent: builtFrom, workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'kirocrew', model: '', triggers: '', session_color: '' },
  ]
}

/** The members roster the switcher reads. `source` is NOT 'builtin', so the
 *  assistant-member hide rule leaves the row in place. */
function members(builtFrom) {
  return [
    { name: 'default', slug: 'default', slot_key: 'chat-1', running: false, kiro_agent: builtFrom, workspace: 'default', memory_store: 'default', source: 'kirocrew', last_active_ts: 0 },
    { name: 'scout', slug: 'scout', slot_key: 'chat-2', running: false, kiro_agent: 'scout', workspace: 'default', memory_store: 'default', source: 'package', last_active_ts: 0 },
  ]
}

const exe = chromiumExecutable()
const { srv, base } = await serveDist()
const browser = await chromium.launch(exe ? { executablePath: exe } : {})

/** Stub /api/** for one (builtFrom, theme) world. */
async function stub(page, builtFrom, theme) {
  await stubDashboardApi(page, {
    theme,
    slots: [{ key: 'chat-1', messages: 0, running: false, agent: 'kirocrew', mode: '' }],
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') { await json(route, KIROCREW_CONFIG_FIXTURE); return true }
      if (path === '/api/agents') { await json(route, { agents: crews(builtFrom), default_agent: 'default' }); return true }
      if (path === '/api/agents/installed') { await json(route, []); return true }
      if (path === '/api/members') { await json(route, { members: members(builtFrom) }); return true }
      if (path === '/api/skills') { await json(route, []); return true }
      return false
    },
  })
  await page.addInitScript(() => { localStorage.setItem('mc-lang', 'en') })
}

const shoot = async (locator, name) => {
  const out = join(OUT, name)
  await locator.scrollIntoViewIfNeeded().catch(() => {})
  await locator.screenshot({ path: out })
  console.log('wrote', out)
}

/** Surfaces 1 + 2: the crew roster card and table, on the agents page. */
async function captureRoster(builtFrom, theme, tag) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, baseURL: base, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  await stub(page, builtFrom, theme)
  await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })

  // The default card in CARDS view. Assert the Built-from value before shooting.
  const card = page.getByRole('button', { name: /Edit (crew|agent|crewmate) default/i }).first()
  await card.waitFor({ state: 'visible', timeout: 15000 })
  if (!(await page.getByText(builtFrom, { exact: true }).count())) {
    throw new Error(`roster card: "${builtFrom}" never rendered as the Built from value`)
  }
  await shoot(card, `card-${tag}.png`)

  // Switch to LIST view and assert the Built-from TABLE cell shows the value.
  // The view toggle is a SegmentedControl: its segments are role=radio.
  const listToggle = page.getByRole('radio', { name: /List/i }).first()
  if (await listToggle.count()) {
    await listToggle.click()
    const row = page.getByTestId('crew-row').first()
    await row.waitFor({ state: 'visible', timeout: 8000 })
    const cellText = (await row.textContent()) || ''
    if (!cellText.includes(builtFrom)) {
      throw new Error(`roster table: row text "${cellText}" missing Built-from "${builtFrom}"`)
    }
    await shoot(row, `table-${tag}.png`)
  } else {
    throw new Error('roster: List view toggle not found')
  }
  await ctx.close()
}

/** Surface 4: the New Crewmate dialog's "Built from" field. On a fresh install
 *  the dialog's default build-from is the frontend DEFAULT_CREWMATE_TEMPLATE
 *  (now `kirocrew-mate-conductor`), rendered as the leading "…(default)" option
 *  and in the field hint. UX needs light+dark stills of this, which the other
 *  three surfaces do not cover. Empty roster so the hero's CTA opens the real
 *  dialog; open Advanced; assert the default label before shooting. */
async function captureDialog(theme, tag) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, baseURL: base, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  await stubDashboardApi(page, {
    theme,
    slots: [],
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') { await json(route, KIROCREW_CONFIG_FIXTURE); return true }
      // Empty roster so the Crewmates page shows its create hero.
      if (path === '/api/agents') { await json(route, { agents: [], default_agent: 'default' }); return true }
      if (path === '/api/agents/installed') { await json(route, []); return true }
      if (path === '/api/members') { await json(route, { members: [] }); return true }
      if (path === '/api/workspaces') { await json(route, { workspaces: [{ name: 'default' }] }); return true }
      // The dialog reads the execution catalog for the "Built from" LIST; the
      // conductor is withheld from it as a selectable template, so it is NOT a
      // row here — the dialog prepends the default (DEFAULT_CREWMATE_TEMPLATE)
      // itself. A couple of ordinary templates stand in for the rest of the list.
      if (path === '/api/agents/catalog') {
        await json(route, {
          agents: [
            { name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' },
            { name: 'kirocrew-research', selection_kind: 'template', kiro_agent: 'kirocrew-research', scope: 'global' },
          ],
          default_agent: 'kirocrew',
        })
        return true
      }
      if (path === '/api/skills') { await json(route, []); return true }
      return false
    },
  })
  await page.addInitScript(() => { localStorage.setItem('mc-lang', 'en') })
  await page.goto('/members', { waitUntil: 'domcontentloaded' })
  // Open the real dialog from the empty-state hero CTA.
  const cta = page.locator('section [data-testid=crewmate-empty-cta]').first()
  await cta.waitFor({ state: 'visible', timeout: 15000 })
  await cta.click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor({ state: 'visible', timeout: 8000 })
  await dlg.getByLabel('Name').fill('Scout')
  // Unfold Advanced so the "Built from" select is on screen.
  await dlg.getByTestId('crewmate-create-advanced-toggle').click()
  const adv = dlg.getByTestId('crewmate-create-advanced')
  await adv.waitFor({ state: 'visible', timeout: 8000 })
  const select = adv.getByRole('combobox', { name: 'Built from' })
  await select.waitFor({ state: 'visible', timeout: 8000 })
  // Assert-then-shoot: the default option really reads the conductor.
  const selText = (await select.textContent()) || ''
  if (!selText.includes(AFTER)) {
    throw new Error(`built-from dialog: select "${selText}" missing default "${AFTER}"`)
  }
  await page.waitForTimeout(500) // disclosure + modal motion settles
  await shoot(dlg, `dialog-builtfrom-${tag}.png`)
  await ctx.close()
}

/** Surface 3: the Crewmate switcher idle-row subtitle shows kiro_agent. */async function captureSwitcher(builtFrom, theme, tag) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, baseURL: base, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  await stub(page, builtFrom, theme)
  // Open a thread so the roster folds into the header switcher chip.
  await page.goto('/members?member=scout', { waitUntil: 'domcontentloaded' })
  const trigger = page.getByTestId('crewmate-switcher')
  await trigger.waitFor({ state: 'visible', timeout: 15000 })
  await trigger.click()
  const list = page.getByTestId('crewmate-switcher-list')
  await list.waitFor({ state: 'visible', timeout: 8000 })
  // The idle "default" row subtitle is its kiro_agent. Assert before shooting.
  const row = list.getByTestId('crewmate-switcher-row').filter({ hasText: builtFrom }).first()
  await row.waitFor({ state: 'visible', timeout: 8000 })
  await shoot(list, `switcher-${tag}.png`)
  await ctx.close()
}

try {
  for (const [builtFrom, phase] of [[BEFORE, 'before'], [AFTER, 'after']]) {
    for (const theme of ['light', 'dark']) {
      const tag = `${phase}-${theme}`
      await captureRoster(builtFrom, theme, tag)
      await captureSwitcher(builtFrom, theme, tag)
    }
  }
  // Surface 4 — the New Crewmate dialog's "Built from" default. The dialog's
  // default is the current frontend constant (the conductor), so this is an
  // after-only surface: light + dark stills UX flagged as missing.
  for (const theme of ['light', 'dark']) {
    await captureDialog(theme, `after-${theme}`)
  }
} finally {
  await browser.close()
  srv.close()
}
