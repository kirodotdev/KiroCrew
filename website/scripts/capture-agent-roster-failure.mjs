/**
 * Screenshot harness for the schedule form's agent picker when the ROSTER FETCH
 * FAILS (#5990).
 *
 * The reported symptom is "only the default agent can be selected". This shoots
 * the state that produces it: `GET /api/agents` fails, so `useAgents` has no
 * roster, while `defaultAgent` comes from the SEPARATE `['default-agent']` query
 * that succeeded — the trigger therefore still reads the default agent's name.
 * Before this change the list beneath it said the italic "No matches", the same
 * thing it says when you filter for a name nobody has, and `SchedulePage` passes
 * a constant `refreshTrigger` so nothing ever re-fetched. Two frames:
 *
 *   1. `failed.png`  — the failure is named and a Retry is offered. The filter
 *      box is deliberately absent: there is nothing to narrow.
 *   2. `recovered.png` — after Retry, with the endpoint healthy, the whole roster.
 *
 * The frames cannot lie: the script ASSERTS the error copy is present and the
 * misleading "No matches" is absent in frame 1, and that every seeded agent is
 * offered in frame 2. Any of those failing exits non-zero and the PNGs are not
 * citable.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through `stubDashboardApi`.
 *
 * Usage: node scripts/capture-agent-roster-failure.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/agent-roster-failure-shots'
const ERROR_COPY = "Couldn't load the agent list."

mkdirSync(OUT, { recursive: true })

const AGENTS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', source: 'builtin', description: 'The built-in agent' },
  { name: 'gpu-research', kiro_agent: 'gpu-research', source: 'package', description: 'Internal research' },
  { name: 'oncall', kiro_agent: 'oncall-agent', source: 'package', description: 'Paging and triage' },
  { name: 'wiki', kiro_agent: 'gpu-wiki', source: 'package', description: 'Wiki edits' },
]

const JOBS = [{
  id: 'j1',
  name: 'Nightly report',
  message: 'Summarise what landed today',
  cron_expr: '0 3 * * *',
  enabled: true,
  agent: '',
}]

// Flipped between the two frames: the roster endpoint is broken for the first,
// healthy for the second, so Retry has something to recover to.
let rosterHealthy = false

const { srv, base } = await serveDist()
const browser = await chromium.launch()

try {
  const context = await browser.newContext({
    // 1x, per the image-read ceiling the sibling harnesses observe.
    viewport: { width: 1500, height: 900 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)

  const FIXTURES = new Map([
    ['/api/crons', { jobs: JOBS }],
    ['/api/cron-folders', []],
    ['/api/crons/history', { runs: [] }],
    ['/api/models', []],
    // The trigger's label survives the roster failure because THIS is what
    // feeds it. Without the split there would be no "only the default agent"
    // to report — the picker would render nameless.
    ['/api/config/default-agent', { default_agent: 'kirocrew' }],
  ])

  await stubDashboardApi(page, {
    localStorageEntries: { 'mc-lang': 'en' },
    extra: async (path, route) => {
      if (path === '/api/agents') {
        if (!rosterHealthy) {
          await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"gateway restarting"}' })
          return true
        }
        await json(route, { agents: AGENTS, default_agent: 'kirocrew' })
        return true
      }
      if (path === '/api/agents/sync') {
        await json(route, { ok: true, synced: [] })
        return true
      }
      if (!FIXTURES.has(path)) return false
      await json(route, FIXTURES.get(path))
      return true
    },
  })

  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.getByRole('table').waitFor({ timeout: 15000 })
  await page.getByText('Nightly report').first().click()

  const dialog = page.locator('[role="dialog"]').first()
  await dialog.getByLabel('Switch agent').waitFor({ timeout: 10000 })
  await dialog.getByLabel('Switch agent').click()

  const picker = page.getByRole('dialog', { name: 'Agent list' })
  await picker.waitFor({ state: 'visible', timeout: 5000 })
  await picker.getByText(ERROR_COPY).waitFor({ timeout: 5000 })
  // The popover fades in, and a page-level shot taken on the appearance of the
  // text alone caught it at opacity 0 — the frame showed the trigger and no
  // popup, which is precisely the evidence it exists to carry.
  await page.waitForTimeout(600)

  const misleading = await picker.getByText('No matches').count()
  await page.screenshot({ path: join(OUT, 'failed.png') })
  await picker.screenshot({ path: join(OUT, 'failed-crop.png') })

  // Retry, with the endpoint healthy this time.
  rosterHealthy = true
  await picker.getByText('Retry').click()
  await picker.getByRole('option').first().waitFor({ timeout: 10000 })
  await page.waitForTimeout(600)

  const offered = await picker.getByRole('option').allTextContents()
  await page.screenshot({ path: join(OUT, 'recovered.png') })
  await picker.screenshot({ path: join(OUT, 'recovered-crop.png') })

  await context.close()

  console.log('frame 1 — "No matches" occurrences:', misleading, '(expected 0)')
  console.log('frame 2 — options offered after Retry:', offered.length, offered.map(t => t.split('\n')[0]))

  const missing = AGENTS.filter(a => !offered.some(t => t.includes(a.name))).map(a => a.name)
  if (misleading !== 0) {
    console.error(`FAIL: the failure frame still shows "No matches" (${misleading}x)`)
    process.exitCode = 1
  }
  if (missing.length) {
    console.error(`FAIL: Retry did not recover the whole roster; missing: ${missing.join(', ')}`)
    process.exitCode = 1
  }
  if (!process.exitCode) console.log('wrote', OUT)
} finally {
  await browser.close()
  srv.close()
}
