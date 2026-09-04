/**
 * Screenshot harness for the crew editor's inline schedule creation.
 *
 * Same gateway-free flow as capture-crew-editor-rail.mjs (real built SPA,
 * fixture-stubbed /api/**). The evidence is the schedules pane's new create
 * affordance: the New schedule button beside Open Schedule, and the expanded
 * inline form with the agent rendered as a pinned value instead of a picker.
 *
 * Usage: node scripts/capture-crew-inline-schedule.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-inline-schedule'

mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'oncall', kiro_agent: 'kirocrew', workspace: 'oncall', memory_store: 'oncall-mem' },
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
        viewport: { width: 1400, height: 980 },
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

      await sheet.locator('[data-testid="crew-rail-schedules"]').click()
      await page.waitForTimeout(250)
      await save('pane-with-add-button')

      await sheet.locator('[data-testid="crew-wake-add"]').click()
      await page.waitForTimeout(250)
      await save('inline-form-open')

      if (theme === 'dark') {
        // The draft guard: a rail pane switch away from typed work asks
        // before destroying it. The guard keys on TYPED work, not on the
        // form being open — a pristine form switches panes unprompted — so
        // the capture types a name first, and the shot shows the confirm
        // over a draft that genuinely has something to lose.
        await sheet.locator('#jobform-name').fill('morning digest')
        await sheet.locator('[data-testid="crew-rail-overview"]').click()
        await page.getByTestId('crew-sched-discard-confirm')
          .waitFor({ state: 'visible', timeout: 5000 })
        await page.waitForTimeout(250)
        await page.screenshot({ path: `${OUT}/${theme}-discard-confirm.png` })
        shot.push(`${theme}-discard-confirm.png`)
      }

      await context.close()

      if (theme === 'dark') {
        // The 320px case the header now survives: below `md` both header
        // actions collapse to labeled icon-only controls, so the heading and
        // the buttons fit the ~216px pane instead of clipping against its
        // overflow-x-hidden ancestor.
        const narrow = await browser.newContext({
          viewport: { width: 320, height: 720 },
          deviceScaleFactor: 2,
        })
        const npage = await narrow.newPage()
        logPageProblems(npage)
        await stubDashboardApi(npage, {
          theme,
          extra: async (path, route) => (await editorApi(path, route))
            || (await crewsApi({ crews: CREWS, defaultAgent: 'kirocrew' })(path, route)),
        })
        await npage.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
        // At 320px the capabilities page renders as a navigation menu; the
        // crews grid lives behind its "Agents" entry.
        const agentsNav = npage.locator('#main-content').getByText('Agents', { exact: true }).first()
        await agentsNav.waitFor({ state: 'visible', timeout: 15000 })
        await agentsNav.click()
        await npage.locator('#main-content [data-testid="crew-card"], #main-content [data-testid="crew-row"]').first()
          .waitFor({ state: 'visible', timeout: 15000 })
        await npage.locator('#main-content [data-testid="crew-card"], #main-content [data-testid="crew-row"]', { hasText: 'oncall' }).first().click()
        const nsheet = npage.getByRole('dialog')
        await nsheet.waitFor({ state: 'visible', timeout: 15000 })
        await nsheet.locator('[data-testid="crew-rail-schedules"]').click()
        await npage.waitForTimeout(400)
        await nsheet.screenshot({ path: `${OUT}/${theme}-narrow-pane-icon-only.png` })
        shot.push(`${theme}-narrow-pane-icon-only.png`)
        await narrow.close()
      }
    }
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(shot.join('\n'))
}

await main()
