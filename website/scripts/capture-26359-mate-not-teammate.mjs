/** Real-browser evidence for #26359: the four mate strings that called a mate
 * a "teammate" now describe it as an always-on AI agent.
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist` (loopback,
 * ephemeral port) with every /api/** call answered by the shared stub, so no
 * gateway is needed. Build first: `npm run build`.
 *
 * Frames per theme (dark, light):
 *   01-mates-empty-<theme>       /members with no mates at 390x844 (the
 *                                roster line is below-md only): empty_body
 *   02-new-goal-<theme>          Radar's Goals tab, New goal dialog: step 1
 *   03-capabilities-mates-<theme> /capabilities?tab=crews with no mates: the
 *                                tab subtitle and the empty-roster line
 *
 * Usage (in website/):
 *   node scripts/capture-26359-mate-not-teammate.mjs ../temp-screenshots/26359
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, KIROCREW_CONFIG_FIXTURE, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/26359')
mkdirSync(OUT, { recursive: true })

const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Watches CI and the issue queue', source: 'kirocrew',
  last_active_ts: Math.floor(Date.now() / 1000) - 30, last_message: 'Nothing new on the nightly.',
}

/** GET /api/members/radar/goals with nothing set yet (MemberGoals in api/client/agents.ts). */
const GOALS = {
  slug: 'radar', member: 'Radar', goals: [], key_results: [], reports: [],
  schedules: { daily: false, weekly: false, monthly: false, heartbeat: false, time: '09:00', timezone: 'UTC', weekday: 1, month_day: 1, interval: 60 },
  removed_key_result: null, removed_goal: null,
  limits: { goal_text_max_chars: 2000, key_result_text_max_chars: 200, key_result_value_max_chars: 40, key_result_reading_max_chars: 120 },
}

let failures = 0
const check = (label, ok) => { console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

const { srv, base: BASE } = await serveDist()
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })

async function open(theme, members, path, viewport = { width: 1280, height: 800 }) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, baseURL: BASE })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    extra: async (p, route) => {
      if (p === '/api/config/kirocrew') { await json(route, KIROCREW_CONFIG_FIXTURE); return true }
      if (p === '/api/members') { await json(route, { members, default_agent: 'kirocrew' }); return true }
      if (p === '/api/agents') { await json(route, { agents: [], default_agent: 'kirocrew' }); return true }
      if (p === '/api/teams') { await json(route, { teams: [] }); return true }
      if (/^\/api\/members\/[^/]+\/thread$/.test(p)) { await json(route, { slot_key: 'member-radar', slug: 'radar', member: 'Radar', created: false }); return true }
      if (/^\/api\/members\/[^/]+\/goals$/.test(p)) { await json(route, GOALS); return true }
      if (/^\/api\/members\/[^/]+\/activity$/.test(p)) { await json(route, { slug: 'radar', member: 'Radar', capped: false, entries: [] }); return true }
      if (/^\/api\/members\/[^/]+\/panel$/.test(p)) { await json(route, { panel: null, html: null }); return true }
      if (/^\/api\/chat\/slots\/[^/]+$/.test(p)) { await json(route, { key: 'member-radar', title: 'Radar', running: false, messages: [] }); return true }
      return false
    },
  })
  await page.goto(path, { waitUntil: 'domcontentloaded' })
  return { ctx, page }
}

const main = (page) => page.locator('#main-content')

for (const theme of ['dark', 'light']) {
  {
    const { ctx, page } = await open(theme, [], '/members', { width: 390, height: 844 })
    const body = main(page).getByText('An always-on AI agent. Give it a job', { exact: false }).first()
    await body.waitFor({ state: 'visible', timeout: 20000 })
    check(`[${theme}] mates empty says always-on AI agent`, await body.isVisible())
    check(`[${theme}] mates empty has no teammate`, !/teammate/i.test(await main(page).innerText()))
    await page.screenshot({ path: resolve(OUT, `01-mates-empty-${theme}.png`) })
    await ctx.close()
  }
  {
    const { ctx, page } = await open(theme, [RADAR], '/members?member=Radar')
    // The header chip (name + presence) opens the profile card that holds Goals.
    await page.locator('button', { hasText: 'Idle' }).first().click()
    await page.getByRole('tab', { name: 'Goals' }).first().click()
    await page.getByTestId('crew-goals-add').first().click()
    const dialog = page.getByTestId('crew-goals-new-goal')
    await dialog.waitFor({ state: 'visible', timeout: 10000 })
    const text = await dialog.innerText()
    check(`[${theme}] new goal step 1 reworded`, text.includes('In your own words, as plainly as you like.'))
    check(`[${theme}] new goal has no teammate`, !/teammate/i.test(text))
    const box = await page.getByRole('dialog').last().boundingBox()
    await page.screenshot({ path: resolve(OUT, `02-new-goal-${theme}.png`), clip: { x: box.x - 40, y: box.y - 40, width: box.width + 80, height: box.height + 80 } })
    await ctx.close()
  }
  {
    const { ctx, page } = await open(theme, [], '/capabilities?tab=crews')
    const sub = main(page).getByText('Always-on AI agents', { exact: false }).first()
    await sub.waitFor({ state: 'visible', timeout: 20000 })
    const empty = main(page).getByText('A mate is an always-on AI agent with its own files and memory.', { exact: false }).first()
    await empty.waitFor({ state: 'visible', timeout: 10000 })
    check(`[${theme}] capabilities subtitle and empty line reworded`, await sub.isVisible() && await empty.isVisible())
    check(`[${theme}] capabilities mates has no teammate`, !/teammate/i.test(await main(page).innerText()))
    await page.screenshot({ path: resolve(OUT, `03-capabilities-mates-${theme}.png`) })
    await ctx.close()
  }
}
await browser.close()
srv.close()
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
