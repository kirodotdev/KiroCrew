/**
 * Screenshot harness for Agent Capabilities > Crews (the renamed first tab).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Proves the two things the rename touches: the side-nav tab label ("Crews")
 * and the tab description under the content header. Fixtures seed a few crews
 * so the table is not an empty state.
 *
 * Usage: node scripts/capture-crews-tab.mjs [outDir] [prefix]
 *   Run against the branch (after) and against a main build (before).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../.github/screenshots/crews-tab'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'oncall', kiro_agent: 'oncall', workspace: 'oncall', memory_store: 'default' },
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'research', memory_store: 'research' },
]

/** Endpoints only the Crews tab needs, layered on top of the shared boot stubs. */
async function crewsApi(path, route) {
  if (path === '/api/agents') {
    return json(route, { agents: CREWS, default_agent: 'kirocrew' }), true
  }
  if (path === '/api/agents/installed') {
    return json(route, [{ name: 'kirocrew' }, { name: 'oncall' }]), true
  }
  if (path === '/api/workspaces') {
    return json(route, {
      workspaces: [{ name: 'default' }, { name: 'oncall' }, { name: 'research' }],
    }), true
  }
  if (path === '/api/config/kirocrew') {
    return json(route, { memory_stores: { default: {}, research: {} } }), true
  }
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px nav type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, { extra: crewsApi })

  await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  // The tab label is the assertion, so fail loudly rather than shoot a blank page.
  const tab = page.locator('#main-content nav').getByRole('button', { name: 'Crews', exact: true })
  await tab.waitFor({ state: 'visible', timeout: 15000 })
  await page.locator('#main-content').getByText('Crews you chat with', { exact: false })
    .first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400) // let the table settle before the shot

  await page.screenshot({ path: `${OUT}/${PREFIX}-crews-tab.png` })
  await page.locator('#main-content nav').screenshot({ path: `${OUT}/${PREFIX}-crews-nav.png` })

  console.log(`wrote ${OUT}/${PREFIX}-crews-tab.png and ${OUT}/${PREFIX}-crews-nav.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
