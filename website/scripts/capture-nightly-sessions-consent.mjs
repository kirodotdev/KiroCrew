/**
 * Evidence for the nightly sessions grant (issue #11560): the backup panel now
 * carries a SECOND nightly switch, off by default, whose hint states what the
 * archive holds, and whose failure leaves the switch where it was.
 *
 * Runs against a Vite dev server with every /api/** call answered from
 * fixtures -- no gateway, no credentials, no real AWS. The toggle POST is the
 * backend's own contract: its own route, reporting `nightlySessions` as its
 * own field.
 *
 *   01-both-switches       both nightly rows: the snapshot switch as it
 *                          shipped, the sessions switch off by default with
 *                          its hint naming the payload
 *   02-sessions-granted    the sessions switch after a granted toggle -- the
 *                          snapshot switch is untouched
 *   03-sessions-failed     the toggle is refused: the notice renders under the
 *                          row and the switch has not moved
 *
 * Usage: node scripts/capture-nightly-sessions-consent.mjs <devServerBase> [outDir] [lang] [theme]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE_URL = process.argv[2]
if (!BASE_URL) {
  console.error('usage: node scripts/capture-nightly-sessions-consent.mjs <devServerBase> [outDir] [lang] [theme]')
  process.exit(2)
}
const OUT = process.argv[3] || '/tmp/nightly-sessions-consent'
const LANG = process.argv[4] || 'en'
const THEME = process.argv[5] || 'dark'
mkdirSync(OUT, { recursive: true })

// One healthy account with a provisioned drive and a granted S3 consent -- the
// baseline every backup frame starts from. Every payload below is derived from
// the five values here rather than repeating them, so the fixture cannot
// disagree with itself about which account the frames are of.
const ACC = '111122223333'
const B = '/api/apps/aws-control'
const REGION = 'us-west-2'
const PROFILE_NAME = 'prod-main'
const ARN = `arn:aws:sts::${ACC}:assumed-role/Admin/dev`

const PROFILE = {
  name: PROFILE_NAME,
  region: REGION,
  account: ACC,
  arn: ARN,
  kind: 'sso',
  identityOk: true,
  detail: '',
  default: true,
}
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
const CONSENT = {
  service: 's3',
  serviceLabel: 'Amazon S3',
  profile: PROFILE_NAME,
  region: REGION,
  account: ACC,
  arn: ARN,
  credentialSource: `profile ${PROFILE_NAME}`,
  identityResolved: true,
  identityDetail: '',
  granted: true,
  reason: '',
  revokedOnAccountChange: false,
  grant: {
    account: ACC,
    region: REGION,
    profile: PROFILE_NAME,
    granted_at: '2026-08-20T09:00:00Z',
  },
}

const SELF_ID = 'a'.repeat(32)
/**
 * What the backend reports. The snapshot grant is already on, so the frames
 * show the two grants held independently rather than moving together.
 * `refuse` is what the next sessions toggle does -- the third frame's whole
 * subject is that a refused grant leaves the switch alone.
 */
const state = { nightly: true, nightlySessions: false, refuse: false, blocked: null }
const backup = () => ({
  nightly: state.nightly,
  nightlySessions: state.nightlySessions,
  nightlySessionsBlocked: state.blocked,
  runs: {}, install: { id: SELF_ID, label: 'this box' },
  remote: {
    snapshot: [], sessions: [],
    installs: [{ id: SELF_ID, label: 'this box', origin: 'self' }],
    others: 0, truncated: false, max: 25,
  },
})

/**
 * The read-only routes, as a table rather than a chain of comparisons: the
 * scenario is about the two WRITE routes below, and a table keeps them the only
 * thing worth reading here.
 */
const reads = () => ({
  [`${B}/accounts`]: () => ACCOUNTS,
  [`${B}/profiles/available`]: () => ({
    profiles: [],
    registeredCount: 1,
    max: 10,
    supported: true,
  }),
  [`${B}/drive/${ACC}`]: () => DRIVE,
  [`${B}/drive/${ACC}/list`]: () => ({ folders: [], files: [], truncated: false }),
  [`${B}/backup/${ACC}`]: backup,
  [`${B}/shares`]: () => ({ shares: [] }),
  [`${B}/library/${ACC}`]: () => ({ artifacts: [] }),
  '/api/aws/consent': (url) => ({
    ...CONSENT,
    service: url.searchParams.get('service') || 's3',
  }),
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
  if (p === `${B}/backup/${ACC}/nightly-sessions`) {
    if (state.refuse) {
      const body = {
        error: 'could not write the nightly sessions setting',
        code: 'config_write_failed',
      }
      json(route, body, 500)
      return true
    }
    state.nightlySessions = route.request().postDataJSON()?.enabled === true
    json(route, { nightlySessions: state.nightlySessions })
    return true
  }
  if (p === `${B}/backup/${ACC}/nightly`) {
    state.nightly = route.request().postDataJSON()?.enabled === true
    json(route, { nightly: state.nightly })
    return true
  }
  return false
}

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
logPageProblems(page)
await stubDashboardApi(page, {
  slots: [], theme: THEME,
  localStorageEntries: { 'mc-lang': LANG },
  extra,
})

/**
 * Shoot the backup section itself, not the whole viewport: the subject is the
 * two adjacent rows and their hints, and a full-page frame renders them too
 * small to read the disclosure the reviewer has to judge.
 */
const shot = async (name) => {
  await page.waitForTimeout(400)
  const row = page.getByTestId('backup-nightly-sessions')
  await row.waitFor({ timeout: 20000 })
  const section = page.locator('section', { has: row }).last()
  const target = (await section.count()) > 0 ? section : page
  await target.screenshot({ path: join(OUT, name) })
  console.log('captured', name)
}

await page.goto(`${BASE_URL}/aws-control/backup`, { waitUntil: 'domcontentloaded' })

// 1. Both grants side by side. The sessions switch is off although the
//    snapshot switch is on, which is the point: they are separate answers.
await page.getByTestId('backup-nightly-sessions').waitFor({ timeout: 30000 })
await shot('01-both-switches.png')

// 2. Grant the sessions archive. Only its own switch moves.
await page.getByTestId('backup-nightly-sessions').getByRole('switch').click()
await page.waitForTimeout(600)
await shot('02-sessions-granted.png')

// 3. Refuse the next write. The notice renders under the row, and the switch
//    stays where the backend actually left it.
state.refuse = true
await page.getByTestId('backup-nightly-sessions').getByRole('switch').click()
await page.getByTestId('backup-nightly-sessions-error').waitFor({ timeout: 10000 })
await shot('03-sessions-failed.png')

// 4. Granted, and not running. The host cannot produce the archive, so the
//    switch reads back as the owner set it AND says nothing is being uploaded --
//    the state a surface reading only the grant would show as healthy.
state.refuse = false
state.nightlySessions = true
// The CODE the route actually sends, not prose: the console localises it, so a
// sentence here would shoot the unrecognised-code fallback instead of the real one.
state.blocked = 'redaction_on'
await page.reload({ waitUntil: 'domcontentloaded' })
await page.getByTestId('backup-nightly-sessions-blocked').waitFor({ timeout: 30000 })
await shot('04-sessions-granted-but-blocked.png')

// 5. The same shape for the condition a second account produces: the grant is
//    settable on any account while the nightly runs for one, so this is a switch
//    that is genuinely on and genuinely idle. The manual run IS per-account, so
//    this variant keeps the next step the host one drops.
state.blocked = 'other_account'
await page.reload({ waitUntil: 'domcontentloaded' })
await page.getByTestId('backup-nightly-sessions-blocked').waitFor({ timeout: 30000 })
await shot('05-sessions-granted-other-account.png')

// 6. The one variant that carries NO next step. On a host that cannot produce the
//    archive the manual run refuses with the same capability answer, so offering
//    it would name a click that fails -- the absence of that sentence is the
//    thing this frame exists to show.
state.blocked = 'host_unsupported'
await page.reload({ waitUntil: 'domcontentloaded' })
await page.getByTestId('backup-nightly-sessions-blocked').waitFor({ timeout: 30000 })
await shot('06-sessions-host-unsupported.png')

await browser.close()
console.log('done ->', OUT)
