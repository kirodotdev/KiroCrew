/**
 * Screenshot harness for the Steering tab's Scope control.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** answered from fixtures, so it needs no gateway and no
 * chat session. That is the only way to photograph this control at all: which
 * scope rows are selectable depends on how many chat slots are bound to a project
 * directory, and the AMBIGUOUS state needs two live chats on two different
 * projects — state a screenshot run cannot produce for real.
 *
 * Each project state gets a FRESH page. Escape on an open SearchableSelect closes
 * the whole dialog (see the note in components/select.tsx) and the overlay
 * teardown then swallows the next click, so reusing one page across states
 * silently photographs the wrong thing.
 *
 * Usage: node scripts/capture-steering-scope.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/steering-scope'
mkdirSync(OUT, { recursive: true })

const PROJECT = '~/work/checkout-service'

/** One steering listing per project state. `files` is held constant so the only
 *  difference between the shots is the thing under review. */
function listing(projectState) {
  return {
    files: [
      {
        key: 'user/global-conventions.md',
        name: 'global-conventions.md',
        rel: 'global-conventions.md',
        source: 'user',
        path: '~/.kiro/steering/global-conventions.md',
        size: 412,
        description: 'Global conventions',
      },
    ],
    roots: [
      { source: 'user', path: '~/.kiro/steering', exists: true },
      { source: 'workspace', path: `${PROJECT}/.kiro/steering`, exists: projectState === 'set' },
    ],
    project: projectState === 'set' ? PROJECT : '',
    project_state: projectState,
  }
}

/** The tab's own always-present control — the readiness locator. Addressed by
 *  testid, never by copy, so a label rename cannot turn this into a blank shot. */
const NEW_FILE_BTN = 'button:has-text("New steering file")'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
  })

  let page = null

  async function open(theme, projectState, { openScope = false } = {}) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme,
      extra: async (path, route) => {
        // Must return TRUTHY to claim the route: `json()` resolves to undefined,
        // so returning it directly lets the stub's catch-all fulfil a second time.
        if (path === '/api/steering') {
          await json(route, listing(projectState))
          return true
        }
        // The tab auto-selects the first row and renders its body, so the detail
        // route needs a real shape — the catch-all's `{}` gives MarkdownRenderer
        // an undefined `content` and takes down the app shell.
        if (path.startsWith('/api/steering/')) {
          await json(route, {
            key: 'user/global-conventions.md',
            content: '# Global conventions\n\nPrefer small, reviewable commits.\n',
            path: '~/.kiro/steering/global-conventions.md',
            source: 'user',
          })
          return true
        }
        return false
      },
    })
    await page.goto(`${base}/capabilities?tab=steering`, { waitUntil: 'domcontentloaded' })
    await page.locator(NEW_FILE_BTN).first().waitFor({ state: 'visible', timeout: 20000 })
    // The listing query resolves after first paint and re-renders the header, so
    // the button present at `visible` is detached out from under a click. Settle
    // first, then re-resolve the locator.
    await page.waitForTimeout(900)
    await page.locator(NEW_FILE_BTN).first().click()
    await page.getByRole('dialog').waitFor({ state: 'visible', timeout: 10000 })
    // Closed by default. The hint line under the select is the affordance being
    // reviewed, and an open dropdown renders straight over it — a shot with the
    // list open cannot evidence a caption about the hint.
    if (openScope) {
      await page.locator('#steering-new-scope').click()
      await page.waitForTimeout(500)
    } else {
      await page.locator('[data-testid="steering-scope-hint"]')
        .waitFor({ state: 'visible', timeout: 10000 })
      await page.waitForTimeout(300)
    }
  }

  for (const theme of ['dark', 'light']) {
    // 01-03: the hint line visible, which is what the captions claim.
    await open(theme, 'set')
    await page.screenshot({ path: join(OUT, `01-scope-project-set-${theme}.png`) })
    console.log('wrote', `01-scope-project-set-${theme}.png`)

    await open(theme, 'none')
    await page.screenshot({ path: join(OUT, `02-scope-no-project-${theme}.png`) })
    console.log('wrote', `02-scope-no-project-${theme}.png`)

    await open(theme, 'ambiguous')
    await page.screenshot({ path: join(OUT, `03-scope-project-conflict-${theme}.png`) })
    console.log('wrote', `03-scope-project-conflict-${theme}.png`)

    // 04: the one shot that needs the list open — the disabled workspace row and
    // its label are themselves the evidence there, not the hint.
    await open(theme, 'none', { openScope: true })
    await page.screenshot({ path: join(OUT, `04-scope-row-disabled-${theme}.png`) })
    console.log('wrote', `04-scope-row-disabled-${theme}.png`)
  }

  await browser.close()
  srv.close()
  console.log('done ->', OUT)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
