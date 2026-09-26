/**
 * Screenshot harness for Dev Fleet served READ-ONLY: a checkout the app may only read.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`, including
 * the app's own `/apps/dev-fleet/api/**` routes. No gateway, no foreign repository
 * on disk, no kiro-cli — the page is driven by exactly the payload the backend
 * sends for that mode, which is what the frames are about.
 *
 * Three frames, each a state a reviewer has to see to judge this change:
 *   1. the whole panel: the read-only banner above the rows, and rows that carry
 *      their state with no action column at all
 *   2. the banner on its own, so the reason text is legible at reading size
 *   3. one row close up, showing the "build unknown" badge where a row of this
 *      product's own fleet would say "not built" and offer Provision
 *
 * Usage: node scripts/capture-dev-fleet-read-only.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/dev-fleet-read-only-shots'
mkdirSync(OUT, { recursive: true })

/**
 * The `/fleet` payload for a configured path that is a git repository without the
 * Kiro Crew markers. `null` is UNKNOWN: every field here that a build or a cutover
 * would answer is one this product's own machinery never runs against this
 * checkout. `read_only_reason` is the backend's own sentence, verbatim.
 */
const FLEET_READ_ONLY = {
  worktrees: [
    {
      name: 'trunk', is_main: true, running: false, has_dist: null, is_live: null,
      is_staged: null, branch: 'trunk', behind: 0, dirty: null,
      last_updated_at: Math.floor(Date.now() / 1000) - 5400, path: '/srv/other-project',
    },
    {
      name: 'wt-parser-rewrite', is_main: false, running: false, has_dist: null,
      is_live: null, is_staged: null, branch: 'feature/parser-rewrite', behind: 4,
      dirty: null, own_commits: 3,
      last_updated_at: Math.floor(Date.now() / 1000) - 26_000,
      path: '/srv/other-project/../wt-parser-rewrite',
    },
    {
      name: 'wt-ci-flake', is_main: false, running: false, has_dist: null,
      is_live: null, is_staged: null, branch: 'fix/ci-flake', behind: 11,
      dirty: null, own_commits: 1,
      last_updated_at: Math.floor(Date.now() / 1000) - 190_000,
      path: '/srv/other-project/../wt-ci-flake',
    },
  ],
  main_repo: '/srv/other-project',
  main_repo_inferred: false,
  base_branch: 'trunk',
  build_pending: null,
  live_state_known: false,
  gateway_service_active: true,
  pods_available: true,
  read_only_reason:
    'read-only: /srv/other-project is a git repository but does not carry the Kiro Crew '
    + 'markers (src/kiro_crew/, pyproject.toml), so Dev Fleet reads its worktrees and '
    + 'refuses every action that would change it. Configured in dev_fleet.repo.',
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openFleet() {
  const context = await browser.newContext({
    viewport: { width: 1420, height: 1000 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)
  const extra = async (path, route) => {
    if (path === '/apps/dev-fleet/api/fleet') {
      await json(route, FLEET_READ_ONLY)
      return true
    }
    if (path === '/apps/dev-fleet/api/disk') {
      await json(route, { total_mb: 40_960, per: {} })
      return true
    }
    // The expanded row's own fetch. Read-only fields only: the detail panel's other
    // half is pod state, which a checkout this app does not build never has.
    if (path === '/apps/dev-fleet/api/worktree') {
      await json(route, {
        branch: 'feature/parser-rewrite',
        dirty: null,
        dirty_tracked: null,
        dirty_untracked: 0,
        own_commits: 3,
        behind: 4,
        pod_running: false,
      })
      return true
    }
    if (path.startsWith('/apps/dev-fleet/api/')) {
      await json(route, {})
      return true
    }
    return false
  }
  await stubDashboardApi(page, { extra })
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.goto(`${base}/dev-fleet`, { waitUntil: 'domcontentloaded' })
  const banner = page.getByTestId('fleet-read-only')
  await banner.waitFor({ timeout: 20_000 })
  await page.getByText('wt-parser-rewrite').first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(900)
  return { context, page, banner }
}

// 1 - the whole panel: banner above rows, and rows with no action column.
{
  const { context, page } = await openFleet()
  const out = join(OUT, '01-read-only-fleet.png')
  await page.screenshot({ path: out })
  console.log('wrote', out)
  await context.close()
}

// 2 - the banner alone, so the reason reads at normal size.
{
  const { context, banner } = await openFleet()
  const out = join(OUT, '02-read-only-banner.png')
  await banner.screenshot({ path: out })
  console.log('wrote', out)
  await context.close()
}

// 3 - the rows close up: "build unknown" where this product's own fleet would say
//     "not built" and offer Provision, and an ACTIONS column that is empty.
{
  const { context, page } = await openFleet()
  const badge = page.getByText('build unknown').first()
  const box = await badge.boundingBox()
  if (!box) throw new Error('could not locate the unknown-build badge')
  const out = join(OUT, '03-read-only-row.png')
  await page.screenshot({
    path: out,
    clip: { x: 280, y: Math.max(0, box.y - 60), width: 1140, height: 160 },
  })
  console.log('wrote', out)
  await context.close()
}

// 4 - a row EXPANDED: the chevron is a read-only row's only affordance, so the panel
//     behind it is where an operator actually lands, and it must carry no Remove.
{
  const { context, page } = await openFleet()
  // The chevron, by its accessible name: clicking the row's NAME toggles nothing.
  await page.getByRole('button', { name: 'Expand' }).last().click()
  await page.getByText('feature/parser-rewrite').first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(900)
  const out = join(OUT, '04-read-only-row-expanded.png')
  await page.screenshot({ path: out })
  console.log('wrote', out)
  await context.close()
}

await browser.close()
srv.close()
