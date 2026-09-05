/**
 * Screenshot harness for the agent create/edit modal declutter:
 *   1. create  — the template field carries the "you can change it later" note,
 *                and the ROUTING section no longer holds a colour picker
 *   2. edit    — the Triggers pane is Triggers alone
 *
 * Gateway-free, the same pattern as the other capture harnesses in this folder:
 * the REAL built SPA (website/dist) behind the shared in-process static server,
 * with every /api/** call answered from fixtures via Playwright route
 * interception. The dashboard API is token-gated and minting a token is refused
 * for an agent, so a live gateway would only ever screenshot the error state.
 *
 * SELF-CHECKS throw rather than write a stale frame: the note must be present in
 * create, and NO session-colour control may survive in either modal.
 *
 * Usage: node scripts/capture-agents-modal-declutter.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { logPageProblems, stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agents-modal-declutter'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

/** One crew carrying a stored session_color, so the frames also prove the
 *  removal does not depend on the value being empty. */
const CREWS = [
  { name: 'default', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Used for all new chats', source: 'user', model: '', triggers: '', session_color: '#6366f1' },
]

/** A plain ARRAY: what /api/agents/installed really answers. */
const INSTALLED = [
  { name: 'kirocrew', description: 'Built-in', source: 'kirocrew', model: '', skills: ['memory'], mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', kirocrew_owned: true },
  { name: 'atlas', description: 'Long-horizon planner', source: 'builtin', model: '', skills: [], mcp_servers: [], filename: 'atlas.json', kirocrew_owned: false },
]

/** The colour picker's own controls, by the accessible names it used to expose.
 *  Asserted absent rather than eyeballed — a frame cannot prove a negative. */
const COLOR_CONTROLS = [/session colour/i, /session color/i, /#rrggbb/i]

async function assertNoColorControl(scope, where) {
  for (const pattern of COLOR_CONTROLS) {
    if (await scope.getByLabel(pattern).count()) {
      throw new Error(`${where}: a session-colour control is still mounted (${pattern})`)
    }
  }
  if (await scope.getByPlaceholder('#rrggbb').count()) {
    throw new Error(`${where}: the session-colour hex input is still mounted`)
  }
}

const { srv, base } = await serveDist()
// Shared resolution: PLAYWRIGHT_CHROMIUM, else the newest cached headless
// shell, else the Playwright pin — so the harness runs without system Chrome.
const executablePath = chromiumExecutable()
const browser = await chromium.launch({ executablePath })

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, baseURL: base })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') {
        await json(route, { ...KIROCREW_CONFIG_FIXTURE })
        return true
      }
      if (path === '/api/agents') {
        await json(route, { agents: CREWS, default_agent: 'default' })
        return true
      }
      if (path === '/api/agents/installed') { await json(route, INSTALLED); return true }
      if (path === '/api/skills') { await json(route, []); return true }
      if (path === '/api/models') { await json(route, { models: [] }); return true }
      return false
    },
  })

  await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').getByText('Agents you chat with', { exact: false })
    .waitFor({ state: 'visible', timeout: 15000 })

  /* ── 1. Create ─────────────────────────────────────────────────────────── */
  await page.getByRole('button', { name: /new (crew|agent)/i }).first().click()
  const create = page.getByRole('dialog', { name: /create/i })
  await create.waitFor({ state: 'visible', timeout: 10000 })

  if (!(await create.getByText(/you can switch this agent's template anytime/i).count())) {
    throw new Error('create modal never rendered the edit-later note')
  }
  await assertNoColorControl(create, 'create modal')

  const out1 = `${OUT}/${PREFIX}-create.png`
  await create.screenshot({ path: out1 })
  console.log(`wrote ${out1}`)
  await page.keyboard.press('Escape')

  /* ── 2. Edit → Triggers pane ───────────────────────────────────────────── */
  const card = page.getByRole('button', { name: /Edit (crew|agent) default/i })
  await card.waitFor({ state: 'visible', timeout: 15000 })
  await card.click()
  const edit = page.getByRole('dialog', { name: /edit (crew|agent)/i })
  await edit.waitFor({ state: 'visible', timeout: 10000 })
  await edit.getByRole('button', { name: /triggers/i }).first().click()

  // The pane must still own Triggers — a frame that lost both controls would
  // pass a bare "no colour picker" check while proving the wrong thing.
  if (!(await edit.getByLabel(/triggers/i).count())) {
    throw new Error('edit modal: the Triggers pane no longer renders its own field')
  }
  await assertNoColorControl(edit, 'edit modal Triggers pane')

  const out2 = `${OUT}/${PREFIX}-edit-triggers.png`
  await edit.screenshot({ path: out2 })
  console.log(`wrote ${out2}`)
} finally {
  await browser.close()
  srv.close()
}
