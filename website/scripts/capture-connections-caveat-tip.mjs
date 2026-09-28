/**
 * Screenshot harness for the Connections card's caveat placement: a
 * pre-registered provider without an OAuth client ("Needs configuration")
 * carries its one-time-setup explanation behind the amber warning triangle
 * beside **Configure OAuth app**, the same PrerequisiteTip the Connect-side
 * caveats use -- not as an always-visible band above the action row.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, every /api/** call answered from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli, no vault.
 *
 * Two scenes, each in light and dark:
 *   1. The gallery at rest with GitHub and Asana unconfigured: badge, value
 *      prop, Documentation and the action row only. The harness fails if the
 *      setup explanation is visible anywhere before the triangle is used.
 *   2. GitHub's triangle pinned by a click: the bubble carries the shared
 *      "Before you connect" heading and the full setup explanation, and the
 *      neighbouring cards are unchanged.
 *
 * Usage: node scripts/capture-connections-caveat-tip.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/connections-caveat-tip'
mkdirSync(OUT, { recursive: true })

const STATUS_ROWS = ['github', 'asana'].map(slug => ({
  slug,
  status: 'not_connected',
  reason: 'client_not_configured',
  grantPresent: false,
  needsClientConfig: true,
}))

const SETUP_COPY = /GitHub needs a one-time setup\. Configure OAuth app opens a guided settings page/

async function capture(theme) {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 880 },
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await page.addInitScript(mode => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme-mode', mode)
  }, theme)

  await stubDashboardApi(page, {
    slots: [],
    // `return json(...), true` -- the comma marks the request handled.
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') return json(route, { connections_ui: true }), true
      if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
      if (path === '/api/mcp' || path === '/api/mcp/probe') return json(route, []), true
      if (path === '/api/connections/status') {
        return json(route, { schema_version: 1, connections: STATUS_ROWS }), true
      }
      if (path === '/api/connections/mint') return json(route, { slug: '', state: 'idle' }), true
      if (path.startsWith('/api/chat/slots/')) {
        return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
      }
      return false
    },
  })

  const shot = async name => {
    await page.waitForTimeout(600)
    await page.screenshot({ path: `${OUT}/${name}-${theme}.png` })
    console.log('wrote', `${OUT}/${name}-${theme}.png`)
  }

  // 1. Gallery at rest: both pre-registered cards in the sixth state, the
  //    explanation nowhere on the page until the triangle is used.
  await page.goto(base + '/capabilities?tab=mcp', { waitUntil: 'domcontentloaded' })
  for (const slug of ['github', 'asana']) {
    await page.locator(`#connection-${slug}[data-state="needs-configuration"]`)
      .waitFor({ state: 'visible', timeout: 20000 })
  }
  const github = page.locator('#connection-github')
  if (await github.getByRole('button', { name: 'Connect', exact: true }).count() !== 0) {
    throw new Error('needs-configuration card still offers Connect')
  }
  if (await page.getByText(SETUP_COPY).count() !== 0) {
    throw new Error('setup explanation is visible at rest -- it belongs behind the triangle')
  }
  if (await page.getByRole('tooltip').count() !== 0) {
    throw new Error('a tooltip is open before any triangle was used')
  }
  const tip = github.getByRole('button', { name: 'GitHub prerequisites' })
  const configure = github.getByRole('link', { name: 'Configure OAuth app' })
  await tip.waitFor({ state: 'visible' })
  await configure.waitFor({ state: 'visible' })
  const tipBox = await tip.boundingBox()
  const configureBox = await configure.boundingBox()
  if (!tipBox || !configureBox || tipBox.x + tipBox.width > configureBox.x + 1) {
    throw new Error('the triangle is not immediately before Configure OAuth app in the action row')
  }
  await github.scrollIntoViewIfNeeded()
  await shot('1-gallery-needs-configuration-at-rest')

  // 2. The triangle pinned by a click: the bubble carries the shared heading
  //    and the whole explanation; nothing else on the page moves.
  await tip.click()
  const bubble = page.getByRole('tooltip')
  await bubble.waitFor({ state: 'visible', timeout: 5000 })
  await bubble.getByText('Before you connect', { exact: true }).waitFor({ state: 'visible' })
  await bubble.getByText(SETUP_COPY).waitFor({ state: 'visible' })
  await shot('2-gallery-needs-configuration-tip-pinned')

  await browser.close()
  srv.close()
}

async function main() {
  for (const theme of ['light', 'dark']) await capture(theme)
}

main().catch(err => { console.error(err); process.exit(1) })
