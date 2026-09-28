/**
 * Screenshot harness for the crew editor's discard guard.
 *
 * Same gateway-free flow as capture-crew-inline-schedule.mjs (real built SPA,
 * fixture-stubbed /api/**). The evidence is the confirm the editor now raises
 * before a dismissal destroys tracked edits: a Triggers change on the routing
 * pane, then Escape, which used to close the sheet and drop the edit silently.
 *
 * Full-page shots rather than element shots: the point of the surface is that
 * the confirm sits OVER the still-open editor, so cropping to either dialog
 * would hide the relationship the change is about.
 *
 * Usage: node scripts/capture-crew-discard-guard.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-discard-guard'

mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  {
    name: 'oncall', kiro_agent: 'kirocrew', workspace: 'oncall',
    memory_store: 'oncall-mem', triggers: 'incidents',
  },
]

/** Endpoints `crewsApi` does not cover but the editor reads. */
const editorApi = async (path, route) => {
  if (path === '/api/crons') {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ jobs: [] }) })
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
        deviceScaleFactor: 1, // keeps both edges under the 2000px review limit
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

      // Named, not getByRole('dialog'): the confirm below is a second dialog and
      // an unnamed locator would go strict-mode ambiguous the moment it opens.
      const sheet = page.getByRole('dialog', { name: /Edit agent/ })
      await sheet.waitFor({ state: 'visible', timeout: 15000 })
      await page.waitForTimeout(500)

      const save = async (name) => {
        await page.screenshot({ path: `${OUT}/${theme}-${name}.png` })
        shot.push(`${theme}-${name}.png`)
      }

      // Triggers is one of the seven fields dirtyPanes tracks and the one the
      // earlier schedule-only guard left unprotected.
      await sheet.locator('[data-testid="crew-rail-routing"]').click()
      await page.waitForTimeout(250)
      await sheet.getByRole('textbox', { name: 'Triggers' }).fill('incidents, prod outages')
      await page.waitForTimeout(250)
      await save('dirty-routing-pane')

      await page.keyboard.press('Escape')
      const confirm$ = page.getByRole('dialog', { name: 'Discard unsaved changes?' })
      await confirm$.waitFor({ state: 'visible', timeout: 5000 })
      await page.waitForTimeout(300)
      await save('discard-confirm')

      // Backing out is the non-destructive default: the editor and the edit
      // both survive, which is the half a still of the confirm cannot show.
      // By testid: the dismiss reads "Keep editing" so it is never confusable
      // with the editor footer's own Cancel sitting right behind it.
      await confirm$.getByTestId('crew-sched-discard-keep').click()
      await confirm$.waitFor({ state: 'hidden', timeout: 5000 })
      await page.waitForTimeout(300)
      await save('kept-after-backing-out')

      // An open schedule draft keeps its own confirm (only it can lock Discard
      // while the draft's create POST is in flight), so with the crew ALSO
      // dirty that dialog is the one shown and has to name both losses.
      await sheet.locator('[data-testid="crew-rail-schedules"]').click()
      await sheet.locator('[data-testid="crew-wake-add"]').click()
      await sheet.locator('#jobform-name').fill('morning digest')
      // The draft flag rides a JobForm effect, so wait for the rail's dirty dot
      // rather than the keystroke: pressing Escape before it lands photographs
      // the generic confirm and silently captures the wrong dialog.
      await sheet.locator('[data-testid="crew-rail-dirty-schedules"]')
        .waitFor({ state: 'visible', timeout: 5000 })
      await page.keyboard.press('Escape')
      // By testid, not by title: the widened dialog deliberately borrows the
      // generic confirm's title, so a name-based locator cannot tell them apart.
      await page.getByTestId('crew-sched-discard-also-crew')
        .waitFor({ state: 'visible', timeout: 5000 })
      await page.waitForTimeout(300)
      await save('schedule-confirm-widened')

      await context.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(shot.join('\n'))
}

await main()
