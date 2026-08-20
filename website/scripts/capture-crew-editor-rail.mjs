/**
 * Screenshot harness for the crew editor's rail + overview diagram.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * One shot per pane, because the change IS the navigation: a single still of the
 * overview would not show that the rail routes anywhere. The fixture deliberately
 * gives `oncall` two active schedules, one paused, and a workspace shared with
 * another crew, so the rail's count, its status dot and the diagram's `shared`
 * tag all have something true to render.
 *
 * Usage: node scripts/capture-crew-editor-rail.mjs [outDir] [prefix]
 *   Run against the branch (after) and against a main build (before). On a main
 *   build there is no rail, so the `before` run shoots the stacked editor once.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../.github/screenshots/crew-editor-rail'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'oncall', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'oncall-mem' },
  // Shares `oncall`'s workspace, which is what lights the rail dot and the tag.
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'research' },
]

const JOBS = [
  {
    id: 'j1', name: 'morning digest', schedule: '0 9 * * *', enabled: true,
    agent: 'oncall', last_run_ts: Date.now() / 1000 - 7200, next_run_ts: Date.now() / 1000 + 54000,
  },
  {
    id: 'j2', name: 'pager poll', schedule: '*/30 * * * *', enabled: true,
    agent: 'oncall', last_run_ts: Date.now() / 1000 - 720, next_run_ts: Date.now() / 1000 + 1080,
  },
  {
    id: 'j3', name: 'weekly report', schedule: '0 17 * * 5', enabled: false,
    agent: 'oncall', last_run_ts: Date.now() / 1000 - 259200,
  },
]

/** Endpoints `crewsApi` does not cover but the editor reads. */
const editorApi = async (path, route) => {
  if (path === '/api/crons') {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ jobs: JOBS }) })
    return true
  }
  if (path === '/api/agents/resolved-model') {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ model: 'claude-opus-5', pinned: false }),
    })
    return true
  }
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shot = []
  try {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({
        viewport: { width: 1400, height: 900 },
        deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
      })
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        theme,
        extra: async (path, route) => (await editorApi(path, route))
          || (await crewsApi({ crews: CREWS, defaultAgent: 'kirocrew' })(path, route)),
      })

      await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
      const main$ = page.locator('#main-content')
      await main$.locator('[data-testid="crew-card"]').first()
        .waitFor({ state: 'visible', timeout: 15000 })

      // `oncall`, not the first card: the default crew has no removal pane and no
      // schedules, so it would evidence the emptiest possible rail.
      await main$.locator('[data-testid="crew-card"]', { hasText: 'oncall' }).first().click()
      const sheet = page.getByRole('dialog')
      await sheet.waitFor({ state: 'visible', timeout: 15000 })
      await page.waitForTimeout(500) // the dialog animates in, then the rail counts land

      const save = async (name) => {
        await page.screenshot({ path: `${OUT}/${PREFIX}-${theme}-${name}.png` })
        shot.push(`${PREFIX}-${theme}-${name}.png`)
      }

      // Guarded so a `before` run against main, which has no rail, still finishes
      // after the one shot it can take.
      const rail = sheet.locator('[data-testid="crew-rail-overview"]')
      if (!(await rail.count())) {
        await save('editor-stacked')
        await context.close()
        continue
      }

      await save('pane-overview')
      for (const key of ['template', 'model', 'place', 'schedules', 'routing', 'danger']) {
        await sheet.locator(`[data-testid="crew-rail-${key}"]`).click()
        await page.waitForTimeout(250)
        await save(`pane-${key}`)
      }

      // The narrow layout: the diagram stacks and drops its connectors, and the
      // rail has to survive a phone width rather than clip.
      await page.setViewportSize({ width: 420, height: 900 })
      await sheet.locator('[data-testid="crew-rail-overview"]').click()
      await page.waitForTimeout(350)
      await save('pane-overview-narrow')

      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(shot.join('\n'))
}

await main()
