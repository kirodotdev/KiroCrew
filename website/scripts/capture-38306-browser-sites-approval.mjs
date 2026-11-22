/**
 * Screenshot harness for the staged "Browsing sites" row on the app detail page.
 *
 * Runs the REAL built SPA behind the shared `serveDist` server and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no dashboard
 * auth, no kiro-cli.
 *
 * The fixture is an installed, enabled app that declares `permissions.browserSites`,
 * which an install never approves, so the row carries `browserSitesGrant.staged`. The
 * page must list each page as a chip under the "Browsing sites" label, with the line
 * that says the app's agent types and clicks there signed in as the owner; after the
 * approve click the enable route answers ok, the consent names exactly those pages,
 * the next read has nothing staged, and the page shows "Approved: ...". Light and dark.
 *
 * Usage: node scripts/capture-38306-browser-sites-approval.mjs [outDir] [--dist=path]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const flag = (name, fallback) => {
  const hit = args.find(a => a.startsWith(`--${name}=`))
  return hit ? hit.slice(name.length + 3) : fallback
}
const OUT = args.find(a => !a.startsWith('--')) || '/tmp/browser-sites-approval-shots'
const DIST = flag('dist', DEFAULT_DIST)
mkdirSync(OUT, { recursive: true })

const APP = 'mood-music'
const SITES = ['https://www.youtube.com/watch', 'https://www.bilibili.com/video/*']

const installedApp = staged => ({
  name: APP, displayName: 'Mood Music', version: '0.3.0', enabled: true,
  installedAt: '2026-11-17T10:00:00Z', origin: 'registry',
  resources: 'gateway', lifecycle: 'gateway',
  browserSitesGrant: staged ? { approved: [], staged: SITES } : { approved: SITES, staged: [] },
  manifest: {
    name: APP, version: '0.3.0', displayName: 'Mood Music',
    description: 'Music picks for how you feel.',
    author: 'iamwhatever', tags: ['music'],
    permissions: { api: ['/api/browser/media'], browserSites: SITES },
    ui: { entry: 'index.js', pages: [{ route: `/${APP}`, label: 'Mood Music' }] },
  },
})

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()

function fail(msg) { throw new Error(`ASSERTION FAILED: ${msg}`) }

async function shoot(mode) {
  let staged = true
  let consent = null
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2, serviceWorkers: 'block', colorScheme: mode,
  })
  const page = await context.newPage()
  page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))
  await stubDashboardApi(page, {
    theme: mode,
    localStorageEntries: { 'mc-onboarded': '1', 'mc-theme-mode': mode },
    extra: async (path, route) => {
      if (path === `/api/apps/${APP}/enable`) {
        consent = route.request().postDataJSON()
        staged = false
        return json(route, { ok: true }), true
      }
      if (path === '/api/apps/registry') return json(route, { apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } }), true
      if (path === '/api/apps/registries') return json(route, { registries: [] }), true
      if (path === `/api/apps/${APP}`) return json(route, installedApp(staged)), true
      if (path === '/api/apps') return json(route, [installedApp(staged)]), true
      if (path === '/api/source/contributors') return json(route, { contributors: [] }), true
      return false
    },
  })

  await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
  const chips = page.getByTestId('staged-browser-sites')
  await chips.waitFor({ timeout: 15000 })
  await page.waitForTimeout(1200)
  for (const site of SITES) {
    if (await chips.getByText(site, { exact: true }).count() !== 1) fail(`staged chip ${site} missing`)
  }
  await page.getByText(/Browsing sites: the app's own agent can open, read, type and click/).first().waitFor()
  await page.screenshot({ path: `${OUT}/browser-sites-staged-${mode}.png`, fullPage: false })
  console.log(`wrote ${OUT}/browser-sites-staged-${mode}.png`)

  await page.getByRole('button', { name: /approve/i }).first().click()
  await page.getByText('Approved:', { exact: false }).first().waitFor({ timeout: 10000 })
  if (JSON.stringify(consent?.grantsConsent?.browserSites) !== JSON.stringify(SITES)) {
    fail(`approve sent browserSites=${JSON.stringify(consent?.grantsConsent?.browserSites)}`)
  }
  if (await page.getByTestId('staged-browser-sites').count() !== 0) fail('staged row still shown after approval')
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/browser-sites-approved-${mode}.png`, fullPage: false })
  console.log(`wrote ${OUT}/browser-sites-approved-${mode}.png`)
  await context.close()
}

await shoot('light')
await shoot('dark')
console.log('OK staged chips, disclosure line, approve consent and approved message in light and dark')

await browser.close()
srv.close()
