/**
 * Screenshot harness for the overview diagram's node navigation.
 *
 * Same gateway-free flow as capture-crew-editor-rail.mjs (real built SPA,
 * fixture-stubbed /api/**). The evidence this feature needs is interaction:
 * a node under hover (the affordance the static diagram lacked), and the pane
 * a click lands on, with the rail row selected — so the shots are one hover
 * still and two post-click stills rather than one per pane.
 *
 * Usage: node scripts/capture-crew-diagram-node-nav.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-diagram-node-nav'

mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'oncall', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'oncall-mem' },
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'research' },
]

const JOBS = [
  {
    id: 'j1', name: 'morning digest', schedule: '0 9 * * *', enabled: true,
    agent: 'oncall', last_run_ts: Date.now() / 1000 - 7200, next_run_ts: Date.now() / 1000 + 54000,
  },
]

const editorApi = async (path, route) => {
  if (path === '/api/crons') {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ jobs: JOBS }) })
    return true
  }
  if (path === '/api/webhooks') {
    // Nothing bound: the ghost node is part of the evidence — it must stay
    // clickable and land on the webhook pane where binding happens.
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ enabled: true, switch_on: true, has_tokens: false, tokens: [] }),
    })
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
        deviceScaleFactor: 1, // sheet-element shots must stay under 2000px on both edges
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

      await main$.locator('[data-testid="crew-card"]', { hasText: 'oncall' }).first().click()
      const sheet = page.getByRole('dialog')
      await sheet.waitFor({ state: 'visible', timeout: 15000 })
      await page.waitForTimeout(500)

      const save = async (name) => {
        await sheet.screenshot({ path: `${OUT}/${theme}-${name}.png` })
        shot.push(`${theme}-${name}.png`)
      }

      // The hover affordance the static diagram lacked.
      await sheet.locator('[data-testid="crew-wire-workspace"]').hover()
      await page.waitForTimeout(150)
      await save('overview-workspace-hover')

      // Click lands on the workspace/memory pane with the rail row selected.
      await sheet.locator('[data-testid="crew-wire-workspace"]').click()
      await page.waitForTimeout(250)
      await save('after-click-workspace')

      // Back to the overview, then the ghost: unbound webhook still navigates.
      await sheet.locator('[data-testid="crew-rail-overview"]').click()
      await page.waitForTimeout(250)
      await sheet.locator('[data-testid="crew-wire-webhook"]').click()
      await page.waitForTimeout(250)
      await save('after-click-webhook-ghost')

      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(shot.join('\n'))
}

await main()
