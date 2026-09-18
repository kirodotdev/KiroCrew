/**
 * Evidence for the overwritten-own-archive restore recovery (issue #10077):
 * a row the ledger draws as 'self' restores unconfirmed, the backend refuses
 * with 409 foreign_install_archive (the drive's bytes moved under the ledger),
 * and the page now opens the confirmation strip instead of dead-ending.
 *
 * Runs against a Vite dev server with every /api/** call answered from
 * fixtures — no gateway, no credentials, no real AWS. The restore POST is the
 * backend's real contract: unconfirmed → 409, foreignOk → 200.
 *
 *   01-refused-strip-open       self row refused → strip opens with the
 *                               overwritten-copy sentence; no error banner
 *   02-collapsed-refusal        disclosure collapsed under the open strip →
 *                               the section notice carries the refusal,
 *                               worded for the row's own origin
 *   03-confirmed-restored       accept → retry carries foreignOk → staged path
 *
 * Usage: node scripts/capture-drive-restore-overwritten.mjs <devServerBase> [outDir] [lang] [theme]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE_URL = process.argv[2]
if (!BASE_URL) {
  console.error('usage: node scripts/capture-drive-restore-overwritten.mjs <devServerBase> [outDir] [lang] [theme]')
  process.exit(2)
}
const OUT = process.argv[3] || '/tmp/drive-restore-overwritten'
const LANG = process.argv[4] || 'en'
const THEME = process.argv[5] || 'dark'
mkdirSync(OUT, { recursive: true })

// One healthy account with a provisioned drive and a granted S3 consent --
// the baseline every backup frame starts from. Inline like the sibling
// capture scripts: each script owns its scenario's fixtures.
const ACC = '111122223333'
const B = '/api/apps/aws-control'
const PROFILE = {
  name: 'prod-main', region: 'us-west-2', kind: 'sso', identityOk: true,
  account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`, detail: '', default: true,
}
const ACCOUNTS = {
  accounts: [{
    account: ACC, name: PROFILE.name, health: 'ok',
    summary: { storage: null, sites: null, tasks: null, costMonthToDate: null },
    profiles: [PROFILE],
  }],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
  generatedAt: '2026-09-11T00:00:00Z',
}
const DRIVE = {
  exists: true, bucket: `kirocrew-drive-${ACC}-usw2`, region: PROFILE.region,
  usage: {
    bytes: 1_181_116_006, objects: 42,
    sections: {
      drive: { objects: 30, bytes: 644_245_094 },
      library: { objects: 4, bytes: 107_374_182 },
      backup: { objects: 8, bytes: 429_496_730 },
    },
  },
}
const CONSENT = {
  service: 's3', serviceLabel: 'Amazon S3',
  profile: PROFILE.name, credentialSource: `profile ${PROFILE.name}`,
  region: PROFILE.region, account: ACC, arn: PROFILE.arn,
  identityResolved: true, identityDetail: '', granted: true, reason: '',
  revokedOnAccountChange: false,
  grant: { account: ACC, region: PROFILE.region, profile: PROFILE.name, granted_at: '2026-08-20T09:00:00Z' },
}

const SELF_ID = 'a'.repeat(32)
const KEY = 'kirocrew/backups/snapshot/2026-09-10T00-00-00Z.tar.zst'
/** The ledger attributes the archive to this install; the drive's bytes disagree. */
const BACKUP = {
  nightly: false, runs: {}, install: { id: SELF_ID, label: 'this box' },
  remote: {
    snapshot: [{ key: KEY, size: 421 * 1024 * 1024, modified: '2026-09-10T00:00:00Z', install: SELF_ID, origin: 'self' }],
    sessions: [],
    installs: [{ id: SELF_ID, label: 'this box', origin: 'self' }],
    others: 1, truncated: false, max: 25,
  },
}

const extra = async (path, route) => {
  const url = new URL(route.request().url())
  const p = url.pathname
  if (!p.startsWith('/api/')) return route.continue(), true
  if (p === `${B}/accounts`) return json(route, ACCOUNTS), true
  if (p === '/api/aws/consent') return json(route, { ...CONSENT, service: url.searchParams.get('service') || 's3' }), true
  if (p === `${B}/profiles/available`) return json(route, { profiles: [], registeredCount: 1, max: 10, supported: true }), true
  if (p === `${B}/drive/${ACC}`) return json(route, DRIVE), true
  if (p === `${B}/drive/${ACC}/list`) return json(route, { folders: [], files: [], truncated: false }), true
  if (p === `${B}/backup/${ACC}`) return json(route, BACKUP), true
  if (p === `${B}/backup/${ACC}/restore`) {
    const body = route.request().postDataJSON()
    // The backend's own judgment: the stored bytes are not what the ledger
    // recorded, so an unconfirmed restore is refused whatever the row said.
    return body?.foreignOk === true
      ? (json(route, { downloaded: true, path: '/home/user/.kiro/crew/restore/snapshot-2026-09-10', bytes: 441_450_496, origin: 'unverified', install: SELF_ID }), true)
      : (json(route, { error: 'archive bytes do not match this install\u2019s upload record', code: 'foreign_install_archive' }, 409), true)
  }
  if (p === `${B}/shares`) return json(route, { shares: [] }), true
  if (p === `${B}/library/${ACC}`) return json(route, { artifacts: [] }), true
  return false
}

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1 })
const page = await ctx.newPage()
logPageProblems(page)
await stubDashboardApi(page, {
  slots: [], theme: THEME,
  localStorageEntries: { 'mc-lang': LANG },
  extra,
})

const shot = async (name) => {
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, name) })
  console.log('captured', name)
}

await page.goto(`${BASE_URL}/aws-control/backup`, { waitUntil: 'domcontentloaded' })

// Open the stored-archive disclosure and refuse the self row's restore.
await page.getByTestId('backup-remote-toggle').click()
await page.getByTestId('backup-archive-row').waitFor({ timeout: 20_000 })
await page.getByTestId('backup-restore').click()

// 1. The 409 opens the strip — the state the page could never reach before —
//    with the overwritten-copy sentence, and no contradictory error banner.
await page.getByTestId('backup-restore-confirm').waitFor({ timeout: 10_000 })
await shot('01-refused-strip-open.png')

// 2. Collapse the disclosure under the open strip: the failure moves to the
//    section-level notice, worded for the row's own origin.
await page.getByTestId('backup-remote-toggle').click()
await page.getByTestId('backup-restore-error').waitFor({ timeout: 10_000 })
await shot('02-collapsed-refusal.png')

// 3. Re-open, accept: the retry carries foreignOk and the staged path renders.
await page.getByTestId('backup-remote-toggle').click()
await page.getByTestId('backup-restore-confirm').waitFor({ timeout: 10_000 })
await page.getByTestId('backup-restore-confirm-yes').click()
await page.getByTestId('backup-restored').waitFor({ timeout: 10_000 })
await shot('03-confirmed-restored.png')

await browser.close()
console.log('done →', OUT)
