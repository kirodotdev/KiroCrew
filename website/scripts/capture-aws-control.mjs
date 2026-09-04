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
 *   files-narrow.png    the Files detail at 320px (pushed level, one back bar)\n *   root-list-narrow.png the 320px grouped root list (the bare path on a phone)
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
// The backup fixture has to behave like a SERVER that claimed a run, not like a
// canned reply: the row's busy state is supposed to come from what the server
// reports, so `jobs` is mutable and the start handler below writes it. A fixture
// that returned a fixed body could not tell an honest adoption from a row that
// just remembered its own click.
const BACKUP = { nightly: false, runs: {}, remote: null, jobs: {} }
// What the START returned, versus what the client was later SERVED. The causal
// phase asserts they are the same run.
const causal = { posted: '', served: '', leaked: [] }
let runSeq = 0
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
  if (/^\/backup\/[^/]+\/run$/.test(app)) {
    // Claim a run and REPORT it, the way the route does. The id it returns is the
    // identity the client must then follow.
    let kind = 'snapshot'
    try {
      kind = JSON.parse(route.request().postData() || '{}').kind || 'snapshot'
    } catch { /* keep the default */ }
    // Hex, like the SDK's own ids -- not a digit-padded string, which would be
    // indistinguishable from an account id to any leak check.
    const runId = (++runSeq).toString(16).padStart(32, 'a')
    const now = '2026-09-02T00:00:00Z'
    BACKUP.jobs = {
      [kind]: {
        active: { run_id: runId, kind, status: 'running', created_at: now, updated_at: now, finished_at: '', error: '' },
        lastFailed: null,
      },
    }
    causal.posted = runId
    return json(route, { started: true, kind, runId })
  }
  if (/^\/backup\/[^/]+$/.test(app)) {
    // The remote half is opt-in, so honour `?remote=1` rather than always paying
    // for the listing -- the harness then exercises the same cost split the
    // product ships.
    const wantRemote = new URL(route.request().url()).searchParams.get('remote') === '1'
    const body = { ...BACKUP, remote: wantRemote ? { snapshot: [], sessions: [] } : null }
    const active = body.jobs?.snapshot?.active ?? body.jobs?.sessions?.active ?? null
    if (active) {
      causal.served = active.run_id
      // The account is the run's dedupe key SERVER-side and must not cross to the
      // client. Assert on the account ids this fixture actually uses and on the
      // field name itself -- an earlier version scanned for any 12-digit run of
      // characters, which the 32-char run id matched, so the check flagged its own
      // fixture and would have passed a real leak just as happily.
      const accounts = ACCOUNTS.accounts.map((a) => a.account).filter(Boolean)
      const flat = JSON.stringify(body.jobs)
      for (const acct of accounts) {
        if (flat.includes(acct)) causal.leaked.push(`account ${acct} present in jobs`)
      }
      if (/dedupe_key/.test(flat)) causal.leaked.push('dedupe_key field present in jobs')
    }
    return json(route, body)
  }
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

// ---- Causal phase: the row's busy state must come from the run the START
// actually created, and must survive a REAL unmount.
//
// Under the rail this is no longer a synthetic re-mount: leaving the pane
// unmounts BackupSection outright, so "the indicator is not component state" is
// demonstrated rather than asserted. The identity check is what makes adoption
// causal instead of coincidental -- a row that merely spins after a click would
// pass a busy-state check while following nothing.
const btnDisabled = () =>
  page.evaluate(() => document.querySelector('[data-testid="backup-run-snapshot"]')?.disabled === true)

console.log(`ASSERT causal row starts idle want=false got=${await btnDisabled()} ${(await btnDisabled()) === false ? 'ok' : 'MISMATCH'}`)
if (await btnDisabled()) failures.push('causal: the row was already busy before any start')

await page.locator('[data-testid="backup-run-snapshot"]').click()
await page.waitForFunction(
  () => document.querySelector('[data-testid="backup-run-snapshot"]')?.disabled === true,
  null,
  { timeout: 5000 },
)

// Leave the pane and come back. This UNMOUNTS the section, so anything the row
// remembered locally is gone; only server state can bring the busy row back.
await openPane('files')
await expectCount('backup-section', 0)
await openPane('backup')
const afterRemount = await btnDisabled()
console.log(`ASSERT causal busy survives a real unmount want=true got=${afterRemount} ${afterRemount ? 'ok' : 'MISMATCH'}`)
if (!afterRemount) failures.push('causal: the run was forgotten across a pane unmount')

const sameRun = causal.posted !== '' && causal.posted === causal.served
console.log(`ASSERT causal client follows the run the POST returned posted=${causal.posted} served=${causal.served} ${sameRun ? 'ok' : 'MISMATCH'}`)
if (!sameRun) failures.push(`causal: posted ${causal.posted || '(none)'} but served ${causal.served || '(none)'}`)

console.log(`ASSERT causal account never reaches the client leaked=${causal.leaked.length} ${causal.leaked.length === 0 ? 'ok' : 'MISMATCH'}`)
if (causal.leaked.length) failures.push(`causal: account id crossed to the client (${causal.leaked.join('; ')})`)

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

// Narrow viewport: iOS push-stack navigation, same shape as settings. The
// pane path deep-links straight to the detail (one back bar); popping lands on
// the grouped root list. The Files toolbar controls must WRAP, not run
// off-screen — measured rather than eyeballed.
await openPane('files')
await page.setViewportSize({ width: 320, height: 900 })
await page.waitForTimeout(400)
await expectCount('aws-pane-detail', 1)
await expectCount('aws-rail', 0)
await page.locator('[data-testid="drive-folder-toggle"]').click()
await page.waitForTimeout(300)
const overflow = await page.evaluate(() => {
  const bad = []
  for (const t of ['drive-folder-name', 'drive-folder-create', 'drive-folder-cancel']) {
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
console.log(`ASSERT narrow-viewport controls-onscreen ${overflow.bad.length === 0 ? 'ok' : 'MISMATCH ' + overflow.bad.join('; ')}`)
if (overflow.bad.length) failures.push(`narrow viewport: ${overflow.bad.join('; ')}`)
await page.screenshot({ path: `${OUT}/files-narrow.png`, fullPage: false })
console.log('shot files-narrow')

// Pop back to the root list: grouped rows, account card, no rail, no detail.
await page.locator('[data-testid="aws-pane-detail"] button').first().click()
await page.waitForTimeout(500)
await expectCount('aws-root-list', 1)
await expectCount('aws-pane-detail', 0)
await expectCount('account-switcher', 1)
for (const pane of ['files', 'library', 'backup', 'shares', 'accounts', 'usage']) {
  await expectCount(`root-${pane}`, 1)
}
await page.screenshot({ path: `${OUT}/root-list-narrow.png`, fullPage: false })
console.log('shot root-list-narrow')

if (unmatched.size) console.log('unmatched /api paths:', [...unmatched].join(', '))
await browser.close()
server.close()
if (failures.length) {
  console.error('harness assertions failed (stale dist, or the UI changed):')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('done')
