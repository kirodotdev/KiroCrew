/**
 * Screenshot harness for the app detail page's source-repo and issue links (#9912).
 *
 * Runs the REAL built SPA behind the shared `serveDist` server and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no dashboard
 * auth, no kiro-cli.
 *
 * The fixture is an installed registry app whose row carries a GitHub repo URL.
 * The Details card must render that repo as an outbound "view source" link (host
 * shown, rel=noopener) plus a "Report an issue" link to the repo's /issues page.
 *
 * Usage: node scripts/capture-9912-app-source-links.mjs [outDir] [--dist=path]
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
const OUT = args.find(a => !a.startsWith('--')) || '/tmp/app-source-links-shots'
const DIST = flag('dist', DEFAULT_DIST)
mkdirSync(OUT, { recursive: true })

const APP = 'pixel-pal'
const REPO = 'https://github.com/octocat/pixel-pal'

const registryRow = {
  name: APP, displayName: 'Pixel Pal', author: 'octocat',
  description: 'A registry app that declares its source repository.',
  tags: ['images'], version: '1.0.0', installed: true, enabled: true,
  origin: 'registry', lifecycle: 'gateway', repo: REPO, _registry: 'sample-registry',
}

const installedApp = {
  name: APP, displayName: 'Pixel Pal', version: '1.0.0', enabled: true,
  installedAt: '2026-07-20T10:00:00Z', origin: 'registry',
  resources: 'gateway', lifecycle: 'gateway', sourceUrl: REPO,
  manifest: {
    name: APP, version: '1.0.0', displayName: 'Pixel Pal',
    description: 'A registry app that declares its source repository.',
    author: 'octocat', tags: ['images'],
    ui: { pages: [{ route: `/${APP}`, label: 'Pixel Pal', iconUrl: 'icon.svg' }] },
  },
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1700, height: 1200 }, deviceScaleFactor: 2, serviceWorkers: 'block',
})
const page = await context.newPage()

const extra = async (path, route) => {
  if (path === '/api/apps/registry') {
    await json(route, { apps: [registryRow], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    return true
  }
  if (path === `/api/apps/${APP}`) {
    await json(route, installedApp)
    return true
  }
  if (path === '/api/apps') {
    await json(route, [installedApp])
    return true
  }
  if (path === '/api/source/contributors') {
    await json(route, { contributors: [] })
    return true
  }
  return false
}

await stubDashboardApi(page, { extra })
page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))

await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
await page.getByText('Pixel Pal').first().waitFor({ timeout: 15000 })
await page.waitForTimeout(1500)

function fail(msg) { throw new Error(`ASSERTION FAILED: ${msg}`) }

const source = page.locator(`a[href="${REPO}"]`).first()
if (await source.count() === 0) fail(`no source link with href ${REPO}`)
if (await source.getAttribute('rel') !== 'noopener noreferrer') fail('source link missing rel=noopener noreferrer')

const issues = page.locator(`a[href="${REPO}/issues"]`).first()
if (await issues.count() === 0) fail(`no "Report an issue" link with href ${REPO}/issues`)
if (await issues.getAttribute('rel') !== 'noopener noreferrer') fail('issue link missing rel=noopener noreferrer')
console.log('OK source link + report-an-issue link present with rel=noopener noreferrer')

// Clip to the Details card so the frame shows exactly the changed surface.
const details = page.getByText('Details').first()
const box = await details.evaluate(el => {
  const card = el.closest('div')?.parentElement ?? el
  const r = card.getBoundingClientRect()
  return { x: Math.max(0, r.x - 8), y: Math.max(0, r.y - 8), width: Math.min(1200, r.width + 16), height: r.height + 16 }
})
await page.screenshot({ path: `${OUT}/app-source-links.png`, clip: box })
console.log(`wrote ${OUT}/app-source-links.png`)

await context.close()
await browser.close()
srv.close()
