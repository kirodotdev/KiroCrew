/**
 * Evidence for the archive count on the backup row (issue #8000): the ledger
 * keeps ONE run per kind, so a second nightly overwrites the first while both
 * archives stay in the drive -- and a row reading only that record reports one
 * archive for a prefix holding several.
 *
 * Runs against a Vite dev server with every /api/** call answered from
 * fixtures -- no gateway, no credentials, no real AWS.
 *
 *   01-count-line          snapshot has 4 recorded archives behind its one run
 *                          line, so the count line renders; sessions has one
 *                          archive behind one run and carries NO second line
 *   02-archive-list-open   the count line clicked: the stored-archive
 *                          disclosure it opens is the surface that can say what
 *                          the drive holds
 *   03-never-backed-up     a kind with no run at all and one recorded archive:
 *                          the count still exceeds what "not backed up yet"
 *                          implies, so the line renders there
 *
 * Usage: node scripts/capture-backup-remembered-archives.mjs <devServerBase> [outDir] [lang] [theme]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE_URL = process.argv[2]
if (!BASE_URL) {
  console.error('usage: node scripts/capture-backup-remembered-archives.mjs <devServerBase> [outDir] [lang] [theme]')
  process.exit(2)
}
const OUT = process.argv[3] || './temp-screenshots/backup-remembered'
const LANG = process.argv[4] || 'en'
const THEME = process.argv[5] || 'dark'
mkdirSync(OUT, { recursive: true })

// One healthy account with a provisioned drive, the baseline every backup frame
// starts from. Everything below is derived from these values rather than
// repeating them, so the fixture cannot disagree with itself about which
// account the frames are of.
const ACC = '111122223333'
const B = '/api/apps/aws-control'
const REGION = 'us-west-2'
const PROFILE_NAME = 'prod-main'
const ARN = `arn:aws:sts::${ACC}:assumed-role/Admin/dev`

// The identity every payload below carries, spelled once. The three fixtures are
// then derived from it, so none of them can name a different account than the
// frames are of. `GRANT_WHO` is the same identity without the assumed-role ARN,
// which the consent GRANT record does not carry.
const GRANT_WHO = { account: ACC, region: REGION, profile: PROFILE_NAME }
const WHO = { ...GRANT_WHO, arn: ARN }

const PROFILE = { ...WHO, name: PROFILE_NAME, kind: 'sso', identityOk: true, detail: '', default: true }

const ACCOUNTS = {
  accounts: [
    {
      account: ACC,
      name: PROFILE_NAME,
      health: 'ok',
      profiles: [PROFILE],
      summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
    },
  ],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
  generatedAt: '2026-09-11T00:00:00Z',
}

// A provisioned drive whose `backup` section already holds several objects: the
// row's claim is about that prefix, so a usage block showing one would contradict
// the frames.
const DRIVE = {
  exists: true,
  region: REGION,
  bucket: `kirocrew-drive-${ACC}-usw2`,
  usage: {
    bytes: 1181116006,
    objects: 42,
    sections: {
      drive: { objects: 30, bytes: 644245094 },
      library: { objects: 4, bytes: 107374182 },
      backup: { objects: 8, bytes: 429496730 },
    },
  },
}

// S3 consent granted, which is the precondition for the backup panel rendering
// anything at all.
const CONSENT = {
  ...WHO,
  service: 's3',
  serviceLabel: 'Amazon S3',
  credentialSource: `profile ${PROFILE_NAME}`,
  identityResolved: true,
  identityDetail: '',
  granted: true,
  reason: '',
  revokedOnAccountChange: false,
  grant: { ...GRANT_WHO, granted_at: '2026-08-20T09:00:00Z' },
}

const SELF_ID = 'a'.repeat(32)
const DAY = 86400000
const at = (daysAgo) => new Date(Date.parse('2026-09-24T09:00:00Z') - daysAgo * DAY).toISOString()

/**
 * The asymmetry the frames exist to show. Snapshot ran four nightlies and the
 * ledger kept the newest ALONE, so its count exceeds what its run line implies.
 * Sessions ran once and holds one archive, so its two lines would agree and the
 * row must NOT carry a second one -- the frame proves the guard, not just the
 * happy path.
 */
const state = {
  runs: {
    snapshot: { key: `snapshots/${SELF_ID}/2026-09-24.tar.gz`, bytes: 418889677, at: at(0) },
    sessions: { key: `sessions/${SELF_ID}/2026-09-24.tar.gz`, bytes: 10485760, at: at(0) },
  },
  remembered: { snapshot: 4, sessions: 1 },
}

// What the disclosure lists: the four snapshot archives the count is counting,
// which is the whole reason the line points here.
const remoteArchives = () => ({
  snapshot: [0, 1, 2, 3].map((n) => ({
    key: `snapshots/${SELF_ID}/${['2026-09-24', '2026-09-23', '2026-09-22', '2026-09-21'][n]}.tar.gz`,
    size: [418889677, 417841101, 416792525, 415743949][n],
    modified: at(n),
    install: SELF_ID,
    origin: 'self',
  })),
  sessions: [
    {
      key: `sessions/${SELF_ID}/2026-09-24.tar.gz`,
      size: 10485760,
      modified: at(0),
      install: SELF_ID,
      origin: 'self',
    },
  ],
  installs: [{ id: SELF_ID, label: 'this box', origin: 'self' }],
  others: 0,
  truncated: false,
  max: 25,
})

const backup = (url) => ({
  nightly: true,
  nightlySessions: true,
  nightlySessionsBlocked: null,
  runs: state.runs,
  rememberedArchives: state.remembered,
  install: { id: SELF_ID, label: 'this box' },
  // The remote listing is the OPT-IN half and costs a LIST, so it is served only
  // when the console actually asks for it -- the same shape as the real route.
  // The client sends `remote=1`, not `remote=true`.
  remote: url.searchParams.get('remote') === '1' ? remoteArchives() : null,
})

/** The read-only routes, as a table: no write route takes part in this scenario. */
const reads = () => ({
  [`${B}/accounts`]: () => ACCOUNTS,
  [`${B}/profiles/available`]: () => ({ profiles: [], registeredCount: 1, max: 10, supported: true }),
  [`${B}/drive/${ACC}`]: () => DRIVE,
  [`${B}/drive/${ACC}/list`]: () => ({ folders: [], files: [], truncated: false }),
  [`${B}/backup/${ACC}`]: backup,
  [`${B}/shares`]: () => ({ shares: [] }),
  [`${B}/library/${ACC}`]: () => ({ artifacts: [] }),
  '/api/aws/consent': (url) => ({ ...CONSENT, service: url.searchParams.get('service') || 's3' }),
})

const extra = async (_path, route) => {
  const url = new URL(route.request().url())
  const p = url.pathname
  if (!p.startsWith('/api/')) {
    await route.continue()
    return true
  }
  const read = reads()[p]
  if (read) {
    json(route, read(url))
    return true
  }
  return false
}

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
logPageProblems(page)
await stubDashboardApi(page, {
  slots: [],
  theme: THEME,
  localStorageEntries: { 'mc-lang': LANG },
  extra,
})

/**
 * Shoot the backup section, not the viewport: the subject is two adjacent rows
 * and whether one of them carries a second line, which a full-page frame renders
 * too small to judge.
 */
const shot = async (name) => {
  await page.waitForTimeout(400)
  const section = page.getByTestId('backup-section')
  await section.waitFor({ timeout: 20000 })
  await section.screenshot({ path: join(OUT, name) })
  console.log('captured', name)
}

/**
 * The stored-archive disclosure is a SIBLING of the backup section, so the frame
 * that has to show what the count line reaches shoots the list itself. A section
 * frame crops above it and would show the chevron turning and nothing else.
 */
const shotArchives = async (name) => {
  await page.waitForTimeout(400)
  const list = page.getByTestId('backup-archive')
  await list.waitFor({ timeout: 20000 })
  // The fixture serves four snapshot archives and one sessions archive, so an
  // empty card means the payload shape is wrong rather than the click missing.
  const rows = await page.getByTestId('backup-archive-row').count()
  if (rows !== 5) throw new Error(`expected 5 archive rows in the disclosure, got ${rows}`)
  await list.screenshot({ path: join(OUT, name) })
  console.log('captured', name)
}

await page.goto(`${BASE_URL}/aws-control/backup`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('backup-row-snapshot').waitFor({ timeout: 30000 })

// 1. The count line on the snapshot row, and its absence on the sessions row.
await page.getByTestId('backup-remembered-snapshot').waitFor({ timeout: 20000 })
if (await page.getByTestId('backup-remembered-sessions').count()) {
  throw new Error('sessions row carries the count line although its count matches its run line')
}
await shot('01-count-line.png')

// 2. The line is the way into the list. Clicking it opens the stored-archive
//    disclosure, which is the only surface that knows what the drive holds --
//    and it lists the four archives the count on the row was counting.
await page.getByTestId('backup-remembered-snapshot').click()
await page.getByTestId('backup-archive').waitFor({ timeout: 20000 })
await shotArchives('02-archive-list-open.png')

// 3. A kind that never ran, holding one recorded archive: "not backed up yet"
//    implies none, so one still exceeds it and the line renders.
state.runs = { sessions: state.runs.sessions }
state.remembered = { snapshot: 1, sessions: 1 }
await page.reload({ waitUntil: 'domcontentloaded' })
await page.getByTestId('backup-remembered-snapshot').waitFor({ timeout: 30000 })
await shot('03-never-backed-up.png')

await browser.close()
console.log('done ->', OUT)
