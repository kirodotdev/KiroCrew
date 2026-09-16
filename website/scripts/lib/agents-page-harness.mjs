/**
 * Shared boot for the Agents-page capture harnesses: serve the built SPA
 * behind the in-process static server, launch the cached Chromium, stub the
 * dashboard API from the given crew fixtures, open the Agents tab and wait
 * for its heading. The caller owns the screenshots and the teardown
 * (`browser.close()` then `srv.close()`), and drives the modals through the
 * two openers below so the roster selectors live in one place.
 */
import { chromium } from 'playwright'
import { serveDist } from './serve-dist.mjs'
import { chromiumExecutable } from './chromium-executable.mjs'
import { logPageProblems, stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE } from './stub-dashboard-api.mjs'

/** A plain ARRAY: what /api/agents/installed really answers. */
export const INSTALLED_FIXTURE = [
  { name: 'kirocrew', description: 'Built-in', source: 'kirocrew', model: '', skills: ['memory'], mcp_servers: ['kirocrew-core'], filename: 'kirocrew.json', kirocrew_owned: true },
  { name: 'atlas', description: 'Long-horizon planner', source: 'builtin', model: '', skills: [], mcp_servers: [], filename: 'atlas.json', kirocrew_owned: false },
]

export async function openAgentsPage({ crews, installed = INSTALLED_FIXTURE }) {
  const { srv, base } = await serveDist()
  // Shared resolution: PLAYWRIGHT_CHROMIUM, else the newest cached headless
  // shell, else the Playwright pin — so the harness runs without system Chrome.
  const executablePath = chromiumExecutable()
  const browser = await chromium.launch({ executablePath })
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
        await json(route, { agents: crews, default_agent: 'default' })
        return true
      }
      if (path === '/api/agents/installed') { await json(route, installed); return true }
      if (path === '/api/skills') { await json(route, []); return true }
      if (path === '/api/models') { await json(route, { models: [] }); return true }
      return false
    },
  })
  await page.goto('/capabilities?tab=crews', { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').getByText('Agents you chat with', { exact: false })
    .waitFor({ state: 'visible', timeout: 15000 })
  return { srv, browser, page }
}

/** Open the create modal from the roster and wait for it to mount. The roster
 *  speaks its own vocabulary ("Add crew member"), older surfaces said "New
 *  agent" — match either so the harness survives the wording, not the pixels. */
export async function openCreateModal(page) {
  await page.getByRole('button', { name: /add crew member|new (crew|agent)/i }).first().click()
  const create = page.getByRole('dialog', { name: /add crew member|create/i })
  await create.waitFor({ state: 'visible', timeout: 10000 })
  return create
}

/** Open the edit modal for the crew named `name`, click the pane whose
 *  rail button matches `paneRe`, and return the dialog locator. */
export async function openEditPane(page, name, paneRe) {
  const card = page.getByRole('button', { name: new RegExp(`Edit (crew|agent) ${name}`, 'i') })
  await card.waitFor({ state: 'visible', timeout: 15000 })
  await card.click()
  const edit = page.getByRole('dialog', { name: /edit (crew|agent)/i })
  await edit.waitFor({ state: 'visible', timeout: 10000 })
  await edit.getByRole('button', { name: paneRe }).first().click()
  return edit
}
