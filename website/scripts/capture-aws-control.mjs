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
  // The account the grant was RECORDED for. The console only shows a receipt
  // whose grant matches the console's own account, so this has to be the first
  // account in ACCOUNTS or the console captures would show no receipt at all.
  grant: { account: '217681647555', region: 'us-west-2', profile: 'personal', granted_at: '2026-08-28T00:00:00+00:00' },
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
// The account list is accounts and nothing else. The confirmation surface moved
// to the console, so a non-zero count here is the regression this pins.
await expectCount('paid-services', 0)
// The rescue mount fires only for a grant no registered account owns. The
// fixture's grant belongs to the first account, so it must stay absent - a hit
// here means the general condition regressed into always-on.
await expectCount('orphan-consent', 0)
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
// The other half of the move: with both fixtures granted, the console is where
// the confirmations are readable and withdrawable.
await expectCount('paid-services', 1)
await expectCount('console-payments-toggle', 0)
await expectCount('console-copy-id', 1)
// The four inline sections are gone; ONE capability row stands for the drive.
await expectCount('console-capabilities', 1)
await expectCount('capability-drive', 1)
await expectCount('drive-section', 0)

// Into the drive page, then into its Files section.
const cap = page.locator('[data-testid="capability-drive"]')
if (await cap.count()) {
  await cap.click()
  await page.waitForTimeout(900)
  await page.screenshot({ path: `${OUT}/drive-root.png`, fullPage: false })
  console.log('shot drive-root')
  // The bucket's three sections are the drive's top level, and the share ledger
  // sits with them because it governs links into all three.
  await expectCount('drive-sections', 1)
  await expectCount('drive-section-drive', 1)
  await expectCount('drive-section-library', 1)
  await expectCount('drive-section-backup', 1)
  await expectCount('access-section', 1)

  const files = page.locator('[data-testid="drive-section-drive"]')
  if (await files.count()) {
    await files.click()
    await page.waitForTimeout(900)
    await page.screenshot({ path: `${OUT}/drive-files.png`, fullPage: false })
    console.log('shot drive-files')
    // Table only: one listing, no gallery and no view toggle anywhere.
    await expectCount('drive-listing', 1)
    await expectCount('drive-view-toggle', 0)
    await expectCount('drive-folder', 1)
    await expectCount('drive-file', 3)
    // Folder creation and its delete confirm are both reachable from here.
    await expectCount('drive-folder-create', 1)
    await expectCount('drive-folder-delete-confirm', 0)
    // Two controls per row: Download plus one overflow trigger. The menu comes
    // from ui/dropdown-menu, which PORTALS its content out of the table - a
    // hand-rolled absolute menu is clipped here, because the scroll container
    // the pinned Actions column needs is overflow-x-auto and that computes
    // overflow-y to auto as well. These counts are the guard: three inline
    // buttons would breach the two-per-row cap, and a non-portalled menu would
    // leave the items unreachable.
    await expectCount('drive-download', 3)
    await expectCount('drive-more', 3)
    await expectCount('drive-folder-more', 1)
    await expectCount('drive-share', 0)
    await expectCount('drive-delete', 0)
    await expectCount('drive-folder-delete', 0)
    // Open the folder overflow and reach the delete through it, then confirm.
    await page.locator('[data-testid="drive-folder-more"]').first().click()
    await page.waitForTimeout(300)
    await expectCount('drive-folder-delete', 1)
    await page.locator('[data-testid="drive-folder-delete"]').first().click()
    await page.waitForTimeout(300)
    await expectCount('drive-folder-delete-confirm', 1)
    // Narrow viewport: the controls must WRAP, not run off-screen. Measured
    // rather than eyeballed - a class change that fails to wrap still produces a
    // plausible screenshot at 1280px.
    await page.setViewportSize({ width: 320, height: 900 })
    await page.waitForTimeout(400)
    const overflow = await page.evaluate(() => {
      const bad = []
      for (const t of ['drive-folder-name', 'drive-folder-create', 'drive-upload-btn',
                       'drive-folder-delete-cancel', 'drive-folder-delete-action']) {
        const el = document.querySelector(`[data-testid="${t}"]`)
        if (!el) { bad.push(`${t}: missing`); continue }
        const r = el.getBoundingClientRect()
        if (r.right > window.innerWidth + 1 || r.left < -1) {
          bad.push(`${t}: ${Math.round(r.left)}..${Math.round(r.right)} outside 0..${window.innerWidth}`)
        }
      }
      return { bad, docScroll: document.documentElement.scrollWidth, win: window.innerWidth }
    })
    console.log(`ASSERT narrow-viewport controls-onscreen ${overflow.bad.length === 0 ? 'ok' : 'MISMATCH ' + overflow.bad.join('; ')}`)
    if (overflow.bad.length) failures.push(`narrow viewport: ${overflow.bad.join('; ')}`)
    await page.screenshot({ path: `${OUT}/drive-narrow.png`, fullPage: false })
    console.log('shot drive-narrow')
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/folder-delete-confirm.png`, fullPage: false })
    console.log('shot folder-delete-confirm')
  } else {
    console.log('NO Files section row found')
  }
} else {
  console.log('NO drive capability row found')
}

if (unmatched.size) console.log('unmatched /api paths:', [...unmatched].join(', '))
await browser.close()
server.close()
if (failures.length) {
  console.error('harness assertions failed (stale dist, or the UI changed):')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('done')
