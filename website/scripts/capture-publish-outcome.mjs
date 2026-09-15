/**
 * Screenshot harness for the Publish panel's outcome-reading fix.
 *
 * Runs the real built SPA behind the shared gateway-free fixture server and
 * drives the artifact detail page's Publish panel through a publish whose
 * response is the SERIALIZED ARTIFACT shape (a `publication` block), which is
 * what `POST /api/artifacts/{slug}/publish` returns.
 *
 * Frames:
 *   01-confirm-step    the confirm step, with the public-exposure warning intact
 *   02-acknowledgment  the blocking acknowledgment, unchanged by this fix
 *   03-published       the result state
 *
 * Run it twice to produce before/after evidence — once on this branch and once
 * with the component reverted (`before` prefix). On `before`, frame 03 shows the
 * defect: a bare red icon with no message, on a publish that succeeded.
 *
 * Usage: node scripts/capture-publish-outcome.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/publish-hub-outcome'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const ARTIFACT = {
  slug: 'engineering-metrics-dashboard',
  name: 'Engineering Metrics Dashboard',
  kind: 'widget',
  source: 'chat',
  description: 'Interactive multi-chart dashboard with filterable bar, line and scatter charts',
  tags: ['visualization', 'charts'],
  version: 1,
  pinned: false,
  created_at: '2026-08-11T02:18:25.000000+00:00',
  updated_at: '2026-08-11T02:18:25.000000+00:00',
}

const RAW_HTML = '<main style="padding:32px"><h1>Engineering Metrics Dashboard</h1></main>'

/** An app-declared publish row, as `GET /api/publish-providers` returns it. */
const PROVIDER = {
  id: 'internal-registry',
  label: 'Internal registry',
  icon: 'Upload',
  endpoint: '/api/apps/publisher/publish',
  kinds: ['widget', 'html', 'markdown'],
  setupRoute: '/publisher',
  app: 'publisher',
  origin: 'app',
  configured: true,
}

/** The serialized artifact `POST /api/artifacts/{slug}/publish` answers with. */
const PUBLISHED_ARTIFACT = {
  ...ARTIFACT,
  publication: {
    provider: 'internal-registry',
    visibility: 'PRIVATE',
    published_by: 'owner',
    view_url: 'https://registry.internal.example/view/3c195b2d',
  },
}

const PREVIEW_MESSAGE =
  'Publishes this artifact as PRIVATE. Change visibility, add shared-with ' +
  'aliases, or unpublish afterwards from its management page.'

async function routes(path, route) {
  if (path === '/api/publish-providers') return json(route, { providers: [PROVIDER] }), true
  if (path === '/api/artifacts') return json(route, { artifacts: [ARTIFACT] }), true
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true

  const m = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
  if (m && decodeURIComponent(m[1]) === ARTIFACT.slug) {
    const rest = m[2] || ''
    if (rest === '') return json(route, { ...ARTIFACT, content: RAW_HTML }), true
    if (rest === '/versions') return json(route, { slug: ARTIFACT.slug, versions: [1] }), true
    if (rest === '/events') return json(route, { slug: ARTIFACT.slug, events: [] }), true
    if (rest === '/comments') return json(route, { comments: [] }), true
    if (rest === '/upstream-status') return json(route, {}), true
  }

  // The publish endpoint is called twice: preview, then confirm.
  if (path === '/api/apps/publisher/publish') {
    let body = {}
    try {
      body = JSON.parse(route.request().postData() || '{}')
    } catch {
      body = {}
    }
    if (!body.confirm) {
      return json(route, {
        requires_confirm: true,
        message: PREVIEW_MESSAGE,
        bytes: 24576,
        content_digest: 'abc123',
      }), true
    }
    return json(route, PUBLISHED_ARTIFACT), true
  }
  return false
}

async function shot(page, name) {
  await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png`, fullPage: false })
  console.log('wrote', `${OUT}/${PREFIX}-${name}.png`)
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
  const browser = await chromium.launch(executablePath ? { executablePath } : {})
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { extra: routes })
  logPageProblems(page)

  await page.goto(base + '/artifacts/' + ARTIFACT.slug, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  await page.getByRole('button', { name: /^Publish$/ }).first().click()
  await page.waitForTimeout(400)
  await page.getByText('Internal registry', { exact: true }).click()
  await page.waitForTimeout(300)

  // Role + accessible name, not `hasText`: these buttons wrap an icon beside the
  // label, so innerText carries whitespace an anchored regex never matches. The
  // LAST match is the panel's own control, the first is the page-level toggle.
  await page.getByRole('button', { name: 'Publish', exact: true }).last().click()
  await page.waitForTimeout(700)
  await shot(page, '01-confirm-step')

  await page.getByText(/Confirm & Publish/).click()
  await page.waitForTimeout(500)
  await shot(page, '02-acknowledgment')

  const ack = page.getByRole('button', { name: /I understand, publish publicly/ })
  if (await ack.count()) await ack.first().click()
  await page.waitForTimeout(900)
  await shot(page, '03-published')

  await browser.close()
  srv.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
