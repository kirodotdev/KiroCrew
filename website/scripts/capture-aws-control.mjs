/**
 * Screenshot harness for the AWS Control app (accounts list + account console).
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call intercepted by Playwright and answered from
 * fixtures — no gateway, no dashboard token. Same technique as
 * capture-apps.mjs, which this is modelled on.
 *
 * Captures:
 *   home.png            the accounts list
 *   account.png         one account's console
 *   account-payments.png  the same console with Payments opened
 *
 * Usage: node scripts/capture-aws-control.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/aws-control-shots'
mkdirSync(OUT, { recursive: true })

// ---- fixtures -------------------------------------------------------------
// Three accounts so the list reads as a list, with one degraded key so the
// health dot is not uniformly green.
const ACCOUNTS = {
  supported: true,
  accounts: [
    {
      account: '217681647555', name: 'personal', health: 'ok',
      profiles: [{ name: 'personal', kind: 'credential-process', region: 'us-west-2', account: '217681647555', default: true, identityOk: true }],
    },
    {
      account: '740412361337', name: 'wombats-alpha', health: 'ok',
      profiles: [
        { name: 'wombats-alpha-admin', kind: 'sso', region: 'us-west-2', account: '740412361337', default: true, identityOk: true },
        { name: 'wombats-alpha-ro', kind: 'sso', region: 'us-east-1', account: '740412361337', default: false, identityOk: true },
      ],
    },
    {
      account: '000417292745', name: 'beetlejuice-auth-syd', health: 'degraded',
      profiles: [{ name: 'beetlejuice-syd', kind: 'sso', region: 'ap-southeast-2', account: '000417292745', default: true, identityOk: false }],
    },
  ],
  totals: { accounts: 3, profiles: 4, profilesHealthy: 3 },
}

const CONSENT = (service) => ({
  service,
  serviceLabel: service === 's3' ? 'Amazon S3 (cloud drive storage)' : 'AWS Cost Explorer',
  granted: true,
  region: 'us-west-2',
  credentialSource: 'profile personal',
  account: '217681647555',
  identityResolved: true,
  revokedOnAccountChange: false,
})

const COSTS = { monthToDate: 2.25, currency: 'USD', fetchedAt: new Date().toISOString(), fresh: true, consentMissing: false }
const DRIVE = { exists: true, bucket: 'kirocrew-drive-7f3a91c4', region: 'us-west-2', usage: { bytes: 44677427, objects: 18 } }
const LISTING = {
  folders: ['demos'],
  files: [
    { key: 'terrace-deck.pdf', size: 2516582, modified: '2026-08-26T09:12:00Z' },
    { key: 'pr-watch-e2e.mp4', size: 19818086, modified: '2026-08-24T18:40:00Z' },
    { key: 'session-storage-demo.mp4', size: 10380902, modified: '2026-08-21T11:05:00Z' },
  ],
}

const BASE = '/api/apps/aws-control'
const LIBRARY = { artifacts: [] }
const BACKUP = { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }
const unmatched = new Set()
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

async function answer(route) {
  const path = new URL(route.request().url()).pathname
  if (path.endsWith('/accounts')) return json(route, ACCOUNTS)
  if (path === '/api/aws/consent') {
    const svc = new URL(route.request().url()).searchParams.get('service') || 's3'
    return json(route, CONSENT(svc))
  }
  // Paths are BASE-prefixed (/api/apps/aws-control/...) and account-scoped, so
  // match on the segment after the base rather than on a suffix.
  const app = path.startsWith(BASE) ? path.slice(BASE.length) : ''
  if (/^\/drive\/[^/]+\/list$/.test(app)) return json(route, LISTING)
  if (/^\/drive\/[^/]+$/.test(app)) return json(route, DRIVE)
  if (/^\/costs\/[^/]+$/.test(app)) return json(route, COSTS)
  if (app === '/profiles/available') return json(route, { supported: true, profiles: [], max: 20 })
  if (/^\/library\/[^/]+$/.test(app)) return json(route, LIBRARY)
  if (/^\/backup\/[^/]+$/.test(app)) return json(route, BACKUP)
  if (app.startsWith('/shares')) return json(route, { shares: [] })
  // ---- dashboard shell, not this app. The shell mounts BEFORE the app page and
  // several of these are consumed as ARRAYS, so a blanket {} crashes the app
  // shell's error boundary ("x.filter is not a function") and the app page never
  // mounts at all. Same fixture set capture-apps.mjs uses, for the same reason.
  if (path === '/api/apps') return json(route, [])
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/status') return json(route, { sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, uptime: 1, version: '0.1.0' })
  if (path === '/api/kiro-prerequisite') return json(route, { installed: true, authenticated: true, ready: true })
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/chat/slots') return json(route, [])
  if (path === '/api/models') return json(route, { models: [], default: 'auto' })
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  // Unknown paths: object-ish names get {}, everything else an array, because a
  // list endpoint answered with an object is what crashes the shell.
  const objectish = /(config|tips|voice|autonudge|branding|status|themes|system)/.test(path)
  unmatched.add(path)
  return json(route, objectish ? {} : [])
}

// ---- run ------------------------------------------------------------------
const { srv: server, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
await page.route('**/api/**', answer)
await page.route('**/api/ws', (route) => route.abort())
page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
  localStorage.setItem('mc-theme-mode', 'dark')
})

await page.goto(`${base}/aws-control`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1200)
await page.screenshot({ path: `${OUT}/home.png`, fullPage: false })
console.log('shot home')
// Assertions must FAIL the run, not just print. A logged count is not a gate:
// a stale dist would exit 0 while every screenshot showed the old page. Home-page
// assertions run WHILE on the home page.
const failures = []
const expectCount = async (t, want) => {
  const got = await page.locator(`[data-testid="${t}"]`).count()
  const ok = got === want
  console.log(`ASSERT ${t} want=${want} got=${got} ${ok ? 'ok' : 'MISMATCH'}`)
  if (!ok) failures.push(`${t}: want ${want}, got ${got}`)
}
await expectCount('aggregate-line', 0)
await expectCount('paid-services', 1)
await expectCount('accounts-search', 1)
await expectCount('accounts-list', 1)

// Into the first account.
const row = page.locator('[data-testid="account-card"]').first()
if (await row.count()) {
  await row.click()
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/account.png`, fullPage: false })
  console.log('shot account')

} else {
  console.log('NO account row found')
}

// Assert on the RENDERED tree, not on the PNG: a stale dist silently produces a
// plausible-looking screenshot of the OLD page. Printed so the caller can diff
// the two runs; `expect` is not available in a plain script.
await expectCount('general-section', 0)
await expectCount('console-ghosts', 0)
await expectCount('console-guard', 0)
await expectCount('console-payments-toggle', 0)
await expectCount('console-copy-id', 1)

if (unmatched.size) console.log('unmatched /api paths:', [...unmatched].join(', '))
await browser.close()
server.close()
if (failures.length) {
  console.error('harness assertions failed (stale dist, or the UI changed):')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('done')
