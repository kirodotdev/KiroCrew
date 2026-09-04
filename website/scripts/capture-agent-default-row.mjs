/**
 * Screenshot harness for the agent picker's default-agent affordance.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * The three frames are three FIXTURE STATES rather than one click sequence, for two
 * reasons: selecting an agent closes the pop-up, and the state the footer row reads —
 * the slot's own agent — is owned by the slots broadcast, which `stubDashboardApi`
 * swallows along with the websocket. Seeding the slot instead shows exactly what a
 * user sees in each steady state:
 *   1. session on the default agent            -> footer reports the state
 *   2. session on some other agent             -> footer offers the write
 *   3. after the write (the one real action)   -> pill moves, footer reports again
 *
 * `PUT /api/config/default-agent` mutates the list's answer because the dashboard
 * re-reads it after a successful write; a static fixture would photograph the old
 * default and misrepresent what the click did.
 *
 * Usage: node scripts/capture-agent-default-row.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/agent-default-row-shots'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'
const OTHER = 'gpu-autosde-critic'
const AGENTS = [
  { name: 'default', source: 'builtin', description: 'The stock crew agent' },
  { name: 'gpu-autosde-analyzer', source: 'package', description: 'Analyzer subagent for gpu-autosde: reads assigned chunk file diffs from disk and generates review comments' },
  { name: OTHER, source: 'package', description: 'Critic subagent for gpu-autosde: validates review comments against the actual diff, checks guideline citations' },
  { name: 'gpu-autosde-fetcher', source: 'package', description: 'Fetcher subagent for gpu-autosde: fetches CR diffs (remote or local), identifies changed files' },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** Open the picker on a fresh page with the slot seeded to `sessionAgent`. */
async function openPicker(sessionAgent) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1 })
  const page = await context.newPage()

  // Mutable so the post-write re-read reflects the write the shot just performed.
  let defaultAgent = 'default'

  /** Each branch AWAITS `json()` then returns true; a falsy return means "not handled". */
  const extra = async (path, route) => {
    if (path === '/api/agents') {
      await json(route, { agents: AGENTS, default_agent: defaultAgent })
      return true
    }
    if (path === '/api/config/default-agent' && route.request().method() === 'PUT') {
      defaultAgent = JSON.parse(route.request().postData() || '{}').agent || defaultAgent
      await json(route, { ok: true })
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: sessionAgent, mode: '' }],
    extra,
  })
  // Pin the locale: without it the SPA picks one from the environment and the shot
  // comes out in whatever language the runner happens to negotiate.
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-lang', 'en')
  }, SLOT)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  // Anchored on the composer control's own label — a loose /agent/i matches the
  // sidebar's "Agent Capabilities" entry first and opens the wrong surface.
  await page.getByRole('button', { name: /^Agent: / }).first().click()
  const picker = page.getByRole('dialog', { name: 'Agent selector' })
  await picker.waitFor({ state: 'visible', timeout: 5000 })
  return { context, picker }
}

const shoot = async (picker, name) => {
  const out = join(OUT, name)
  await picker.screenshot({ path: out })
  console.log('wrote', out)
}

// 1 — the session is on the agent that already holds the default.
{
  const { context, picker } = await openPicker('default')
  await shoot(picker, '01-picker-default-agent-active.png')
  await context.close()
}

// 2 + 3 — the session is on another agent, so the footer offers the write; then the
// write lands and the footer reports the new state.
{
  const { context, picker } = await openPicker(OTHER)
  await picker.getByRole('button', { name: `Set ${OTHER} as default agent for new sessions` })
    .waitFor({ state: 'visible', timeout: 5000 })
  await shoot(picker, '02-footer-offers-the-write.png')

  await picker.getByRole('button', { name: `Set ${OTHER} as default agent for new sessions` }).click()
  await picker.getByRole('button', { name: 'Default agent for new sessions' })
    .waitFor({ state: 'visible', timeout: 5000 })
  await shoot(picker, '03-default-written.png')
  await context.close()
}

await browser.close()
srv.close()
