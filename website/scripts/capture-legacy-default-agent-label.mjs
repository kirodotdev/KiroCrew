/**
 * Screenshot harness for #6495: slots and cron jobs created without a pinned
 * agent must be labeled with the RESOLVED default agent's alias, not the
 * literal 'default'.
 *
 * Surface: the Schedule page's agent column cell and its tooltip for an
 * agent-less cron job, next to a pinned job so the two states read
 * differently on one screen. (The agents-rail half of the fix renders inside
 * the Worlds scenes' canvas/SVG layers — pinned by unit tests instead.)
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * SELF-CHECKS (throw = no stale frame): the agent-less cron row's cell and
 * title carry the resolved alias and the pinned row keeps its own agent
 * (asserted per named row against this fixture).
 *
 * Usage: node scripts/capture-legacy-default-agent-label.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/legacy-default-agent-label'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

// The resolved default agent's alias. A distinctive name so the frames cannot
// be mistaken for the literal fallback.
const DEFAULT_AGENT = 'atlas'

const JOBS = [
  {
    // The legacy shape under test: no pinned agent — resolves the current
    // default at run time, so the accurate label is the resolved alias.
    id: 'job-1', name: 'Nightly report', schedule: 'every 1d',
    message: 'Summarise yesterday and post the digest.',
    enabled: true, agent: '',
    last_status: 'ok', last_run_ts: now - 3600, next_run_ts: now + 7200,
  },
  {
    // Control row: an explicit pin must be untouched by the resolved default.
    id: 'job-2', name: 'Feed poller', schedule: 'every 300s',
    message: 'Poll the release feed and flag regressions.',
    enabled: true, agent: 'coder',
    last_status: 'ok', last_run_ts: now - 240, next_run_ts: now + 60,
  },
]

/** Read the agent-column cell for a job row, or throw naming the row. */
async function agentCell(page, jobName) {
  const row = page.getByRole('row').filter({ hasText: jobName })
  const cell = row.getByText(DEFAULT_AGENT).or(row.getByText('coder')).or(row.getByText('default')).first()
  await cell.waitFor({ timeout: 10000 })
  const text = await cell.textContent()
  const title = await cell.locator('xpath=ancestor::td[1]').getAttribute('title')
  return { text, title }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 900 },
    deviceScaleFactor: 1,
  })
  const page = await context.newPage()
  logPageProblems(page)

  const ROUTES = new Map([
    ['/api/crons', { jobs: JOBS }],
    ['/api/cron-folders', []],
    ['/api/crons/history', { runs: [] }],
    ['/api/agents', { agents: [{ name: DEFAULT_AGENT }, { name: 'coder' }], default_agent: DEFAULT_AGENT }],
    ['/api/config/default-agent', { default_agent: DEFAULT_AGENT }],
    ['/api/models', []],
    ['/api/spawn/list', []],
  ])
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (!ROUTES.has(path)) return false
      await json(route, ROUTES.get(path))
      return true
    },
  })

  // --- Schedule page: agent column cell + tooltip ---
  await page.goto(base + '/schedule', { waitUntil: 'domcontentloaded' })
  await page.getByRole('table').waitFor({ timeout: 15000 })
  await page.getByText('Nightly report').first().waitFor()
  await page.waitForTimeout(400)

  const legacy = await agentCell(page, 'Nightly report')
  // In before mode (pre-fix build) the defect itself is the expectation: the
  // agent-less row shows the literal 'default'. After the fix it must show
  // the resolved alias.
  // After the fix the agent-less row reads '<alias> · default' — the alias
  // plus the inherited-default marker (issue #6495 + UX review).
  const expected = PREFIX === 'before' ? 'default' : `${DEFAULT_AGENT} · default`
  if (legacy.text !== expected) {
    throw new Error(`agent-less row cell shows '${legacy.text}', expected '${expected}' (${PREFIX} mode)`)
  }
  if (!legacy.title || !legacy.title.includes(expected)) {
    throw new Error(`agent-less row tooltip missing '${expected}': ${legacy.title}`)
  }
  const pinned = await agentCell(page, 'Feed poller')
  if (pinned.text !== 'coder') {
    throw new Error(`pinned row cell shows '${pinned.text}', expected 'coder'`)
  }
  await page.screenshot({ path: `${OUT}/${PREFIX}-schedule.png` })
  await page.locator('table').first().screenshot({ path: `${OUT}/${PREFIX}-schedule-table.png` })

  // The agents-rail half of the fix (useAgentSync) renders inside the Worlds
  // scenes' canvas/SVG layers, which a DOM-driven harness cannot self-check;
  // that surface is pinned by the useAgentSync unit tests instead.

  await browser.close()
  srv.close()
  console.log(`wrote 2 frames to ${OUT}/ with prefix ${PREFIX}`)
}

main().catch(err => { console.error(err); process.exit(1) })
