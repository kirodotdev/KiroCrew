/**
 * Screenshot harness for pre-registered OAuth clients (Settings → OAuth Apps
 * and the gallery's "Needs configuration" card).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, every /api/** call answered from fixtures via Playwright route
 * interception -- gateway-free, no kiro-cli, no vault.
 *
 * Five scenes, each in light and dark:
 *   1. Capabilities → Connections with GitHub and Asana unconfigured. The
 *      status feed marks both rows `needsClientConfig`, so the cards render the
 *      sixth state with the "Configure OAuth app" link instead of Connect.
 *   2. Settings → OAuth Apps with GitHub configured (id + secret set) and
 *      Asana not, so both card badges appear in one frame.
 *   3. The same tab with GitHub's client id owned by the environment.
 *   4. The gallery with GitHub configured: Connect is back, and the pinned
 *      prerequisite tip carries the pre-registered copy.
 *   5. Settings → Secrets listing the vault half of GitHub's client as a
 *      read-only row that hands off to OAuth Apps.
 *
 * Usage: node scripts/capture-connections-oauth-clients.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/connections-oauth-clients'
mkdirSync(OUT, { recursive: true })

const CLIENTS = [
  {
    slug: 'github',
    confidential: true,
    redirect_uri: 'http://127.0.0.1:48101/callback',
    registration_guide: 'oauth-app-registration/github.md',
    client_id: 'Iv1.8a61f9b3a7aba766',
    client_id_source: 'config',
    client_secret_set: true,
    client_secret_source: 'vault',
    configured: true,
  },
  {
    slug: 'asana',
    confidential: true,
    redirect_uri: 'http://127.0.0.1:48102/callback',
    registration_guide: 'oauth-app-registration/asana.md',
    client_id: null,
    client_id_source: null,
    client_secret_set: false,
    client_secret_source: null,
    configured: false,
  },
]

const STATUS_ROWS = ['github', 'asana'].map(slug => ({
  slug,
  status: 'not_connected',
  reason: 'client_not_configured',
  grantPresent: false,
  needsClientConfig: true,
}))

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
      if (path === '/api/secrets') {
        return json(route, {
          names: [],
          managed: [{ name: 'CONNECTIONS_GITHUB_CLIENT_SECRET', kind: 'connections_client_secret', host: 'github' }],
        }), true
      }
      if (path === '/api/connections/oauth-clients') {
        return json(route, { schema_version: 1, clients: CLIENTS }), true
      }
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

  // 1. Gallery: both pre-registered cards in the sixth state.
  await page.goto(base + '/capabilities?tab=mcp', { waitUntil: 'domcontentloaded' })
  for (const slug of ['github', 'asana']) {
    await page.locator(`#connection-${slug}[data-state="needs-configuration"]`)
      .waitFor({ state: 'visible', timeout: 20000 })
  }
  const github = page.locator('#connection-github')
  if (await github.getByRole('button', { name: 'Connect', exact: true }).count() !== 0) {
    throw new Error('needs-configuration card still offers Connect')
  }
  const link = github.getByRole('link', { name: 'Configure OAuth app' })
  const href = await link.getAttribute('href')
  if (!href || !href.startsWith('/settings/connections')) {
    throw new Error(`configure link does not target Settings → OAuth Apps: ${href}`)
  }
  await github.scrollIntoViewIfNeeded()
  await shot('1-gallery-needs-configuration')

  // 2. Settings → OAuth Apps, reached through the card's own link so the
  //    deep link is exercised too; GitHub configured, Asana not.
  await link.click()
  await page.getByText('http://127.0.0.1:48101/callback').first().waitFor({ state: 'visible', timeout: 20000 })
  await page.getByText('http://127.0.0.1:48102/callback').first().waitFor({ state: 'visible', timeout: 20000 })
  if (await page.getByText('Configured', { exact: true }).count() !== 1) {
    throw new Error('expected exactly one Configured badge (GitHub)')
  }
  if (await page.getByText('Needs configuration', { exact: true }).count() !== 1) {
    throw new Error('expected exactly one Needs configuration badge (Asana)')
  }
  await shot('2-settings-connections')

  // 2b. Arming Remove on the configured GitHub card: the danger confirm with
  //     its consequence line, then Cancel backs out without a request.
  const ghCard = page.locator('[data-setting-id="connections-oauth-client-github"]')
  await ghCard.getByRole('button', { name: 'Remove the GitHub OAuth app' }).click()
  await ghCard.getByRole('button', { name: 'Remove OAuth app' }).waitFor({ state: 'visible' })
  await ghCard.getByText(/The client ID and the stored secret are deleted/).waitFor({ state: 'visible' })
  await shot('6-settings-remove-armed')
  await ghCard.getByRole('button', { name: 'Cancel' }).click()

  // 3. Same tab with GitHub's client id owned by the environment: the input is
  //    disabled and names the variable, the secret stays dashboard-editable.
  CLIENTS[0].client_id_source = 'env'
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.getByText(/Set by the environment variable KIROCREW_CONNECTIONS_GITHUB_CLIENT_ID/)
    .first().waitFor({ state: 'visible', timeout: 20000 })
  const envInput = page.locator('#connection-github, [data-setting-id="connections-oauth-client-github"]')
    .getByLabel('Client ID')
  if (!(await envInput.isDisabled())) throw new Error('env-owned client id is not disabled')
  await shot('3-settings-connections-env-override')
  CLIENTS[0].client_id_source = 'config'

  // 4. Gallery again with GitHub CONFIGURED: the card is back to its ordinary
  //    not-connected state with Connect, and the prerequisite tip (pinned by
  //    a click so it survives the screenshot) carries the pre-registered copy.
  STATUS_ROWS[0].needsClientConfig = false
  STATUS_ROWS[0].reason = 'no_grant'
  await page.goto(base + '/capabilities?tab=mcp', { waitUntil: 'domcontentloaded' })
  const configured = page.locator('#connection-github[data-state="not-connected"]')
  await configured.waitFor({ state: 'visible', timeout: 20000 })
  await configured.getByRole('button', { name: 'Connect', exact: true }).waitFor({ state: 'visible' })
  await page.locator('#connection-asana[data-state="needs-configuration"]').waitFor({ state: 'visible' })
  await configured.scrollIntoViewIfNeeded()
  await configured.getByRole('button', { name: 'GitHub prerequisites' }).click()
  await page.getByText(/The GitHub consent page names that app; approve it to continue/).first()
    .waitFor({ state: 'visible', timeout: 5000 })
  await shot('4-gallery-configured-with-prerequisite')
  STATUS_ROWS[0].needsClientConfig = true
  STATUS_ROWS[0].reason = 'client_not_configured'

  // 5. Settings → Secrets: the vault half of GitHub's client is listed as a
  //    read-only, provider-labelled row that hands off to OAuth Apps.
  await page.goto(base + '/settings/secrets', { waitUntil: 'domcontentloaded' })
  await page.getByText('CONNECTIONS_GITHUB_CLIENT_SECRET').first().waitFor({ state: 'visible', timeout: 20000 })
  const manage = page.getByRole('link', { name: 'Manage in OAuth Apps' })
  const manageHref = await manage.getAttribute('href')
  if (!manageHref || !manageHref.startsWith('/settings/connections')) {
    throw new Error(`secrets row does not hand off to Settings → OAuth Apps: ${manageHref}`)
  }
  await shot('5-settings-secrets-managed-row')

  await browser.close()
  srv.close()
}

async function main() {
  for (const theme of ['light', 'dark']) await capture(theme)
}

main().catch(err => { console.error(err); process.exit(1) })
