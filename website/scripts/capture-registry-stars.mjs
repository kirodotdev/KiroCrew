/**
 * Screenshot harness for the GitHub star count on third-party registry apps.
 *
 * Runs the REAL built SPA behind the shared `serveDist` server and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no dashboard
 * auth, no kiro-cli.
 *
 * Fixture shape: two registry rows — a git-type third-party app carrying the
 * publisher-baked `stargazersCount`, and a builtin without the field — so the
 * list frame shows the star badge on the third-party row AND its absence on
 * the builtin row. A second frame captures the detail page (hero subtitle +
 * Details card row).
 *
 * Asserts before shooting: exactly one star badge in the list (third-party
 * only), the compact count on both surfaces, and the exact count in the
 * Details card.
 *
 * Usage: node scripts/capture-registry-stars.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/registry-stars-shots'
mkdirSync(OUT, { recursive: true })

const REPO = 'https://example.invalid/octocat/todo-ledger'

/** Git-type third-party row with a publisher-baked star count. */
const thirdParty = {
  name: 'todo-ledger', displayName: 'Todo Ledger', author: 'octocat',
  description: 'A third-party app installed from a git repository.',
  tags: ['productivity'], version: '1.2.0', installed: false,
  origin: 'registry', repo: REPO, provenance: 'official',
  stargazersCount: 15300,
}

/** Builtin row — never carries the field, so the badge must be absent. */
const builtin = {
  name: 'research-lab', displayName: 'Research Lab', author: 'Kiro Crew',
  description: 'A built-in app that lives in the product repository.',
  tags: ['research'], version: '3.1.0', installed: true, enabled: true,
  origin: 'builtin', provenance: 'builtin', verified: true,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: 'dark' })
page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))

/** Each branch AWAITS `json()` then returns true; falsy = not handled. */
const extra = async (path, route) => {
  if (path === '/api/apps/registry') {
    await json(route, { apps: [thirdParty, builtin], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    return true
  }
  if (path === '/api/apps/todo-ledger') {
    // Not installed: the detail page must fall through to the registry row.
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
    return true
  }
  if (path === '/api/apps') {
    await json(route, [])
    return true
  }
  return false
}
await stubDashboardApi(page, { extra })

function fail(msg) { throw new Error(`ASSERTION FAILED: ${msg}`) }

// --- Frame 1: Discover list, both rows ------------------------------------
await page.goto(`${base}/apps`, { waitUntil: 'domcontentloaded' })
await page.getByText('Todo Ledger').first().waitFor({ timeout: 15000 })
await page.getByText('Research Lab').first().waitFor({ timeout: 15000 })

// The third-party row shows the compact count; en compact of 15300 is 15.3K.
await page.getByText('15.3K').first().waitFor({ timeout: 5000 })
// Exactly one star badge on screen: the builtin row must not carry one.
const badges = await page.getByLabel('GitHub stars').count()
if (badges !== 1) fail(`expected exactly 1 star badge (third-party only), found ${badges}`)

await page.screenshot({ path: `${OUT}/discover-list-stars.png` })
console.log(`wrote ${OUT}/discover-list-stars.png`)

// --- Frame 2: detail page, hero subtitle + Details card --------------------
await page.goto(`${base}/apps/detail/todo-ledger`, { waitUntil: 'domcontentloaded' })
await page.getByText('Todo Ledger').first().waitFor({ timeout: 15000 })
await page.getByText('15.3K').first().waitFor({ timeout: 5000 })
// Details card renders the exact locale-formatted count.
await page.getByText('15,300').first().waitFor({ timeout: 5000 })
await page.screenshot({ path: `${OUT}/detail-page-stars.png`, fullPage: true })
console.log(`wrote ${OUT}/detail-page-stars.png`)

await browser.close()
srv.close()
console.log('capture ok')
