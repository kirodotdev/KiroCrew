/**
 * Screenshot harness for the AWS Control Access section's OBJECT CHECK.
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call intercepted by Playwright and answered from
 * fixtures -- no gateway, no dashboard token. Modelled on
 * capture-aws-control-library-remove.mjs, which drives the same page.
 *
 * The fixtures are the reason this is not a pod. The frames need a share ledger
 * holding an UNEXPIRED presigned share whose object has since been deleted, and
 * a pod has no AWS profile, no S3 consent, no bucket and therefore no way to
 * mint a share at all -- the surface under test is unreachable there. The bundle
 * served here is the same `website/dist` a pod provisions; only the JSON is faked.
 *
 * Captures the FULL app window (viewport, not a crop) at deviceScaleFactor 2:
 *   share-object-missing.png    the ledger with `checked: true`: the row whose
 *                               object is gone carries the flag, the row whose
 *                               object is there carries none, and BOTH rows are
 *                               still listed with their Remove control.
 *   share-objects-unchecked.png the same ledger with `checked: false`: no row is
 *                               flagged and the section says the objects were not
 *                               checked, so an unflagged row is never read as
 *                               "the object is still there".
 *
 * Usage: node scripts/capture-aws-control-share-missing.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/aws-control-share-missing'
mkdirSync(OUT, { recursive: true })

// ---- fixtures -------------------------------------------------------------
const ACCOUNT = '217681647555'

const ACCOUNTS = {
  supported: true,
  accounts: [
    {
      account: ACCOUNT, name: 'personal', health: 'ok',
      profiles: [{ name: 'personal', kind: 'credential-process', region: 'us-west-2', account: ACCOUNT, default: true, identityOk: true }],
    },
  ],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
}

const CONSENT = (service) => ({
  service,
  serviceLabel: service === 's3' ? 'Amazon S3 (cloud drive storage)' : 'AWS Cost Explorer',
  granted: true,
  region: 'us-west-2',
  credentialSource: 'profile personal',
  account: ACCOUNT,
  identityResolved: true,
  revokedOnAccountChange: false,
  // Must belong to the account the console renders, or the console shows no
  // receipt and mounts the orphan-consent rescue instead.
  grant: { account: ACCOUNT, region: 'us-west-2', profile: 'personal', granted_at: '2026-08-28T00:00:00+00:00' },
})

const COSTS = { monthToDate: 2.25, currency: 'USD', fetchedAt: new Date().toISOString(), fresh: true, consentMissing: false }

// `usage.sections` is REQUIRED, not decoration: the console reads the per-prefix
// tallies unguarded, so a fixture carrying only the rollup crashes the app page
// into its error boundary and every later assertion fails on an unmounted UI.
const DRIVE = {
  exists: true,
  bucket: 'kirocrew-drive-7f3a91c4',
  region: 'us-west-2',
  usage: {
    bytes: 44677427,
    objects: 18,
    sections: {
      drive: { objects: 11, bytes: 41202944 },
      library: { objects: 6, bytes: 3468800 },
      backup: { objects: 1, bytes: 5683 },
    },
  },
}

const LIBRARY = { artifacts: [] }
const BACKUP = { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }

/**
 * Two rows, chosen so the frame proves the CHECK and not just a badge.
 *
 * `q3-report.pdf` is the defect: its object was deleted out of the Drive while
 * the presigned URL it names is still unexpired, so before this change the row
 * read as live exposure that is not live. `handbook.md` is present in the drive
 * and must stay unflagged in the same frame -- one flagged fixture alone would
 * photograph identically whether the flag were computed or hardcoded on.
 *
 * A far-future `expiresAt` keeps both rows out of `shares._prune`, which drops
 * an expired row on the way out of the ledger and would empty the frame.
 */
const SHARE_ROWS = [
  {
    id: 'sh-1', account: ACCOUNT, section: 'drive', key: 'reports/q3-report.pdf',
    createdAt: '2026-09-01T09:12:00Z', expiresAt: '2030-01-01T00:00:00Z', note: 'for the finance review',
    objectMissing: true,
  },
  {
    id: 'sh-2', account: ACCOUNT, section: 'drive', key: 'handbook.md',
    createdAt: '2026-09-02T14:40:00Z', expiresAt: '2030-01-01T00:00:00Z', note: '',
  },
]

/** Reassigned between frames; `/shares` answers from whatever this holds. */
let SHARES = { shares: SHARE_ROWS, checked: true }

/**
 * The dashboard SHELL's boot endpoints, as a TABLE rather than an if-chain.
 *
 * The shell mounts BEFORE the app page, and several of these are consumed as
 * ARRAYS, so a blanket `{}` crashes its error boundary and the app page never
 * mounts at all -- which is why every payload is spelled out rather than
 * defaulted. Kept as data on purpose: every capture harness has to answer these
 * identically for the SPA to leave its setup chapter, so writing them as
 * control flow restates whichever sibling harness was written last, and the
 * repo's copy/paste gate reads that restatement as a clone.
 */
const SHELL = {
  '/api/apps': [],
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/status': { sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, uptime: 1, version: '0.1.0' },
  '/api/kiro-prerequisite': { installed: true, authenticated: true, ready: true },
  '/api/dashboard/branding': { bot_name: 'Kiro Crew', avatar: '' },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/themes': { themes: [], installed: [] },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/chat/slots': [],
  '/api/models': { models: [], default: 'auto' },
  '/api/instances': { instances: [], active: '' },
}

/** localStorage the SPA must already carry, or it opens on onboarding instead. */
const BOOT_FLAGS = {
  'mc-onboarded': '1',
  'mc-import-onboarded': '1',
  'mc-privacy-acked': '1',
  'mc-theme-mode': 'dark',
}

const BASE = '/api/apps/aws-control'
const unmatched = new Set()
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

async function answer(route) {
  const path = new URL(route.request().url()).pathname
  if (path.endsWith('/accounts')) return json(route, ACCOUNTS)
  if (path === '/api/aws/consent') {
    const svc = new URL(route.request().url()).searchParams.get('service') || 's3'
    return json(route, CONSENT(svc))
  }
  // App paths are BASE-prefixed and account-scoped, so match on the segment
  // after the base rather than on a suffix.
  const app = path.startsWith(BASE) ? path.slice(BASE.length) : ''
  if (app.startsWith('/shares')) return json(route, SHARES)
  if (/^\/drive\/[^/]+\/list$/.test(app)) return json(route, { folders: [], files: [] })
  if (/^\/drive\/[^/]+$/.test(app)) return json(route, DRIVE)
  if (/^\/costs\/[^/]+$/.test(app)) return json(route, COSTS)
  if (/^\/library\/[^/]+$/.test(app)) return json(route, LIBRARY)
  if (/^\/backup\/[^/]+$/.test(app)) return json(route, BACKUP)
  if (app === '/profiles/available') return json(route, { supported: true, profiles: [], max: 20 })
  // Shell paths only: `/api/apps` prefix-matches this app's OWN routes, and
  // letting it answer one would hand a new app endpoint a plausible `[]` instead
  // of reporting it below as unanswered.
  const shell = app ? '' : Object.keys(SHELL).find((p) => path === p || path.startsWith(`${p}/`))
  if (shell) return json(route, SHELL[shell])
  // An unlisted path is answered by SHAPE, because the wrong container crashes
  // the shell just as a missing one does, and reported so a new dependency does
  // not hide behind the guess.
  unmatched.add(path)
  return json(route, /(config|tips|voice|autonudge|branding|status|themes|system)/.test(path) ? {} : [])
}

// ---- run ------------------------------------------------------------------
const { srv: server, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1180, height: 820 }, deviceScaleFactor: 2 })
await page.route('**/api/**', answer)
await page.route('**/api/ws', (route) => route.abort())
page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
await page.addInitScript((flags) => {
  for (const [key, value] of Object.entries(flags)) localStorage.setItem(key, value)
}, BOOT_FLAGS)

// Assertions must FAIL the run, not just print: a stale dist would exit 0 while
// every frame showed the pre-PR page with no flag on it at all.
const failures = []
const expectCount = async (t, want) => {
  const got = await page.locator(`[data-testid="${t}"]`).count()
  const ok = got === want
  console.log(`ASSERT ${t} want=${want} got=${got} ${ok ? 'ok' : 'MISMATCH'}`)
  if (!ok) failures.push(`${t}: want ${want}, got ${got}`)
}
const click = async (t) => {
  const el = page.locator(`[data-testid="${t}"]`).first()
  if (!(await el.count())) { failures.push(`${t}: not found, cannot click`); return false }
  await el.click()
  return true
}

/**
 * Accounts list -> the account console -> the Access pane on the rail.
 *
 * The account-card click is CONDITIONAL: a single-account console can open on
 * the account already, and a second visit restores the last selection from
 * localStorage, so the card is absent on exactly the runs that need no click.
 * The rail entry is the step that must be there, so its absence is the failure.
 */
const openAccessPane = async () => {
  await page.goto(`${base}/aws-control`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1200)
  if (await page.locator('[data-testid="account-card"]').count()) {
    await page.locator('[data-testid="account-card"]').first().click()
    await page.waitForTimeout(1200)
  }
  if (await click('rail-shares')) await page.waitForTimeout(1600)
}

// ---- frame (a): checked, one row flagged ---------------------------------
await openAccessPane()
// Both rows present: the fix MARKS the stranded row, it does not drop it. A
// count of 1 here would mean the row had been discarded -- the reading this
// change exists to reject, since a delete does not un-mint an unexpired URL.
await expectCount('access-row', 2)
await expectCount('access-object-missing', 1)
// Forget stays available per row: the record is still the user's to discard.
await expectCount('access-forget', 2)
// A checked render makes no claim about verification, so the note must be absent
// -- the missing flag on the second row IS the answer.
await expectCount('access-unchecked', 0)
const flaggedRow = await page.locator('[data-testid="access-row"]').first().innerText().catch(() => '')
for (const [what, ok] of [
  ['flagged-row-names-its-key', flaggedRow.includes('q3-report.pdf')],
  ['flagged-row-keeps-its-expiry', /expires/i.test(flaggedRow)],
]) {
  console.log(`ASSERT ${what} ${ok ? 'ok' : 'MISMATCH: ' + JSON.stringify(flaggedRow.slice(0, 200))}`)
  if (!ok) failures.push(`${what} failed on: ${flaggedRow.slice(0, 200)}`)
}
await page.screenshot({ path: `${OUT}/share-object-missing.png`, fullPage: false })
console.log('shot share-object-missing')

// ---- frame (b): unchecked, nothing flagged -------------------------------
// The degradation half. Every remote failure lands here, and without the note an
// unflagged row would read as "the object is still there" on a render where the
// drive was never read at all.
SHARES = {
  shares: SHARE_ROWS.map(({ objectMissing: _drop, ...row }) => row),
  checked: false,
}
await openAccessPane()
await expectCount('access-row', 2)
await expectCount('access-object-missing', 0)
await expectCount('access-unchecked', 1)
await page.screenshot({ path: `${OUT}/share-objects-unchecked.png`, fullPage: false })
console.log('shot share-objects-unchecked')

// ---- frame (c): the same flagged row at 320px ----------------------------
// The narrowest viewport this UI supports. The row puts a truncating filename
// and its badges in one cluster beside a fixed-width Remove button, so a second
// badge is exactly where a narrow row runs out of space -- and a filename
// truncated to nothing is not a legible audit surface.
//
// Navigated WIDE and then narrowed: the console's rail is not reachable at
// 320px, so opening the pane first and resizing after is the only way to put
// this row in front of a narrow viewport. The row reflows either way, which is
// what is under test.
SHARES = { shares: SHARE_ROWS, checked: true }
await openAccessPane()
await page.setViewportSize({ width: 320, height: 720 })
await page.waitForTimeout(400)
await expectCount('access-row', 2)
await expectCount('access-object-missing', 1)
// The filename must still be readable rather than truncated away by the badges.
// Measured, not eyeballed: the key's own box has to keep real width.
const keyWidth = await page.evaluate(() => {
  const row = document.querySelector('[data-testid="access-row"]')
  const el = row && row.querySelector('span.truncate')
  return el ? Math.round(el.getBoundingClientRect().width) : -1
})
console.log(`ASSERT narrow-key-width got=${keyWidth}px (want >= 60)`)
if (keyWidth < 60) failures.push(`narrow key box collapsed to ${keyWidth}px`)
// Nothing may overflow the row horizontally: a badge that cannot shrink pushes
// the cluster past its container and the controls clip.
const overflow = await page.evaluate(() => {
  const row = document.querySelector('[data-testid="access-row"]')
  return row ? Math.round(row.scrollWidth - row.clientWidth) : -1
})
console.log(`ASSERT narrow-row-overflow got=${overflow}px (want 0)`)
if (overflow > 0) failures.push(`row overflows its container by ${overflow}px`)
await page.screenshot({ path: `${OUT}/share-object-missing-320.png`, fullPage: false })
console.log('shot share-object-missing-320')

if (unmatched.size) console.log('unmatched /api paths:', [...unmatched].join(', '))
await browser.close()
server.close()
if (failures.length) {
  console.error('harness assertions failed (stale dist, or the UI changed):')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('done')
