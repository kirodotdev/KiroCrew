/**
 * Screenshot harness for the AWS Control app (flat-rail IA).
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call intercepted by Playwright and answered from
 * fixtures — no gateway, no dashboard token. Same technique as
 * capture-apps.mjs, which this is modelled on.
 *
 * Captures:
 *   files.png           the landing pane — the drive's Files section with the rail
 *   switcher.png        the account switcher menu open
 *   library.png         the artifact library pane
 *   shares.png          the share-links pane
 *   accounts.png        the Accounts & credentials pane
 *   usage.png           the Usage & costs pane
 *   files-narrow.png    the Files pane at 320px (rail flattened to a strip)
 *
 * Usage: node scripts/capture-aws-control.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/aws-control-shots'
mkdirSync(OUT, { recursive: true })

// ---- fixtures -------------------------------------------------------------
// Three accounts so the switcher reads as a switcher, with one degraded key so
// the health dot is not uniformly green.
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
  // The account the grant was RECORDED for. The usage pane only shows a receipt
  // whose grant matches the SELECTED account, so this has to be the first
  // account in ACCOUNTS or the usage capture would show no receipt at all.
  grant: { account: '217681647555', region: 'us-west-2', profile: 'personal', granted_at: '2026-08-28T00:00:00+00:00' },
})

const COSTS = { monthToDate: 2.25, currency: 'USD', fetchedAt: new Date().toISOString(), fresh: true, consentMissing: false }
// `sections` is REQUIRED by DriveUsage: the rail counts and the storage meter
// both read it, so a fixture without it crashes the shell.
const DRIVE = {
  exists: true, bucket: 'kirocrew-drive-7f3a91c4', region: 'us-west-2',
  usage: {
    bytes: 44677427, objects: 18,
    sections: {
      drive: { objects: 4, bytes: 32715570 },
      library: { objects: 6, bytes: 11157402 },
      backup: { objects: 8, bytes: 804455 },
    },
  },
}
const LISTING = {
  folders: ['demos'],
  files: [
    { key: 'terrace-deck.pdf', size: 2516582, modified: '2026-08-26T09:12:00Z' },
    { key: 'pr-watch-e2e.mp4', size: 19818086, modified: '2026-08-24T18:40:00Z' },
    { key: 'session-storage-demo.mp4', size: 10380902, modified: '2026-08-21T11:05:00Z' },
  ],
}
// Two live shares so the ledger pane and the rail count both read non-empty.
const SHARES = {
  shares: [
    { id: 'sh-1', account: '217681647555', section: 'drive', key: 'terrace-deck.pdf', createdAt: '2026-08-30T10:00:00Z', expiresAt: '2026-09-03T10:00:00Z', note: 'contractor review' },
    { id: 'sh-2', account: '217681647555', section: 'library', key: 'launch-dashboard-v3', createdAt: '2026-09-01T08:00:00Z', expiresAt: '2026-09-02T08:00:00Z', note: '' },
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
  if (app.startsWith('/shares')) return json(route, SHARES)
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

// Assertions must FAIL the run, not just print. A logged count is not a gate:
// a stale dist would exit 0 while every screenshot showed the old page.
const failures = []
const expectCount = async (t, want) => {
  const got = await page.locator(`[data-testid="${t}"]`).count()
  const ok = got === want
  console.log(`ASSERT ${t} want=${want} got=${got} ${ok ? 'ok' : 'MISMATCH'}`)
  if (!ok) failures.push(`${t}: want ${want}, got ${got}`)
}
const expectText = async (t, want) => {
  const got = ((await page.locator(`[data-testid="${t}"]`).first().textContent()) || '').trim()
  const ok = got === want
  console.log(`ASSERT ${t} text want=${want} got=${got} ${ok ? 'ok' : 'MISMATCH'}`)
  if (!ok) failures.push(`${t}: want text ${want}, got ${got}`)
}
const openPane = async (pane, settle = 900) => {
  await page.locator(`[data-testid="rail-${pane}"]`).click()
  await page.waitForTimeout(settle)
}

await page.goto(`${base}/aws-control`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1200)

// The landing IS the drive: rail beside the Files listing, no account chooser
// in the way and no intermediate console level anywhere.
await expectCount('aws-rail', 1)
await expectCount('account-switcher', 1)
for (const p of ['files', 'library', 'backup', 'shares', 'accounts', 'usage']) {
  await expectCount(`rail-${p}`, 1)
}
await expectCount('rail-meta', 1)
await expectCount('drive-section', 1)
await expectCount('console-crumb', 0)
await expectCount('drive-crumb-back', 0)
// Rail counts come from the drive's usage split (and the share ledger's own
// endpoint), so they must agree with the fixture, not with each other.
await expectText('rail-files-count', '4')
await expectText('rail-library-count', '6')
await expectText('rail-backup-count', '8')
await expectText('rail-shares-count', '2')
// The switcher card names the persisted (here: first) account.
await expectText('switcher-name', 'personal')
await page.screenshot({ path: `${OUT}/files.png`, fullPage: false })
console.log('shot files')

// Files pane behavior (unchanged from the pre-rail listing, re-pinned here).
await expectCount('drive-listing', 1)
await expectCount('drive-folder', 1)
await expectCount('drive-file', 3)
// Folder creation is a DISCLOSURE: collapsed, the toolbar carries the toggle +
// Upload and no name input; opening it swaps Upload out and reveals the input
// + Create + Cancel. Assert both states rather than an always-visible input.
await expectCount('drive-folder-toggle', 1)
await expectCount('drive-folder-name', 0)
await expectCount('drive-upload-btn', 1)
await page.locator('[data-testid="drive-folder-toggle"]').click()
await page.waitForTimeout(300)
await expectCount('drive-folder-name', 1)
await expectCount('drive-folder-create', 1)
await expectCount('drive-folder-cancel', 1)
await expectCount('drive-upload-btn', 0)
await page.locator('[data-testid="drive-folder-cancel"]').click()
await page.waitForTimeout(300)
await expectCount('drive-folder-name', 0)
await expectCount('drive-upload-btn', 1)
// Two controls per row: Download plus one overflow trigger (portalled menu).
await expectCount('drive-download', 3)
await expectCount('drive-more', 3)
await expectCount('drive-folder-more', 1)
await expectCount('drive-share', 0)
await expectCount('drive-delete', 0)

// The account switcher open: every RESOLVED account plus the manage entry.
await page.locator('[data-testid="account-switcher"]').click()
await page.waitForTimeout(400)
await expectCount('switcher-option', 3)
await expectCount('switcher-manage', 1)
await page.screenshot({ path: `${OUT}/switcher.png`, fullPage: false })
console.log('shot switcher')
// Switching re-points every pane at the chosen account.
await page.locator('[data-testid="switcher-option"][data-account="740412361337"]').click()
await page.waitForTimeout(600)
await expectText('switcher-name', 'wombats-alpha')
// Back to the first account so the paid-service receipts (granted for it)
// render on the usage pane below.
await page.locator('[data-testid="account-switcher"]').click()
await page.waitForTimeout(300)
await page.locator('[data-testid="switcher-option"][data-account="217681647555"]').click()
await page.waitForTimeout(600)

// The other panes, one rail click each — no descent, no breadcrumbs.
await openPane('library')
await expectCount('library-section', 1)
await page.screenshot({ path: `${OUT}/library.png`, fullPage: false })
console.log('shot library')

await openPane('backup')
await expectCount('backup-section', 1)

await openPane('shares')
await expectCount('access-section', 1)
await expectCount('access-row', 2)
await page.screenshot({ path: `${OUT}/shares.png`, fullPage: false })
console.log('shot shares')

// Accounts & credentials: rows select (check on the current one), connections
// for the selected account, no receipts here (they live on Usage & costs), and
// the orphan rescue stays absent while a registered account owns the grant.
await openPane('accounts')
await expectCount('accounts-pane', 1)
await expectCount('accounts-list', 1)
await expectCount('account-card', 3)
await expectCount('account-current', 1)
await expectCount('accounts-connections', 1)
await expectCount('add-accounts', 1)
await expectCount('paid-services', 0)
await expectCount('orphan-consent', 0)
await page.screenshot({ path: `${OUT}/accounts.png`, fullPage: false })
console.log('shot accounts')

// Usage & costs: the bill, the storage meter, and both consent receipts
// (granted for the selected account, no ask on screen).
await openPane('usage')
await expectCount('usage-pane', 1)
await expectCount('console-stats', 1)
await expectCount('console-cost-value', 1)
await expectCount('usage-storage', 1)
await expectCount('paid-services', 1)
await expectCount('costs-consent-gate', 0)
await page.screenshot({ path: `${OUT}/usage.png`, fullPage: false })
console.log('shot usage')

// Narrow viewport: the rail flattens to a horizontal strip and the Files
// toolbar controls must WRAP, not run off-screen. Measured rather than
// eyeballed — a class change that fails to wrap still produces a plausible
// screenshot at 1280px.
await openPane('files')
await page.setViewportSize({ width: 320, height: 900 })
await page.waitForTimeout(400)
await expectCount('aws-rail', 1)
await page.locator('[data-testid="drive-folder-toggle"]').click()
await page.waitForTimeout(300)
const overflow = await page.evaluate(() => {
  const bad = []
  for (const t of ['drive-folder-name', 'drive-folder-create', 'drive-folder-cancel', 'account-switcher']) {
    const el = document.querySelector(`[data-testid="${t}"]`)
    if (!el) { bad.push(`${t}: missing`); continue }
    const r = el.getBoundingClientRect()
    if (r.right > window.innerWidth + 1 || r.left < -1) {
      bad.push(`${t}: ${Math.round(r.left)}..${Math.round(r.right)} outside 0..${window.innerWidth}`)
    }
  }
  return { bad, docScroll: document.documentElement.scrollWidth, win: window.innerWidth }
})
await page.locator('[data-testid="drive-folder-cancel"]').click()
await page.waitForTimeout(300)
const overflowCollapsed = await page.evaluate(() => {
  const bad = []
  for (const t of ['drive-folder-toggle', 'drive-upload-btn']) {
    const el = document.querySelector(`[data-testid="${t}"]`)
    if (!el) { bad.push(`${t}: missing`); continue }
    const r = el.getBoundingClientRect()
    if (r.right > window.innerWidth + 1 || r.left < -1) {
      bad.push(`${t}: ${Math.round(r.left)}..${Math.round(r.right)} outside 0..${window.innerWidth}`)
    }
  }
  return bad
})
overflow.bad.push(...overflowCollapsed)
console.log(`ASSERT narrow-viewport controls-onscreen ${overflow.bad.length === 0 ? 'ok' : 'MISMATCH ' + overflow.bad.join('; ')}`)
if (overflow.bad.length) failures.push(`narrow viewport: ${overflow.bad.join('; ')}`)
await page.screenshot({ path: `${OUT}/files-narrow.png`, fullPage: false })
console.log('shot files-narrow')

if (unmatched.size) console.log('unmatched /api paths:', [...unmatched].join(', '))
await browser.close()
server.close()
if (failures.length) {
  console.error('harness assertions failed (stale dist, or the UI changed):')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('done')
