/**
 * The aws-control app's error surfaces, for a UI review: every failure the app
 * can show renders through the shared error notice with its agent hand-off, and
 * the states below used to render either a bare red paragraph or nothing at all.
 *
 * Runs against a Vite dev server (no built dist needed) with every /api/** call
 * answered from fixtures — no gateway, no credentials, no real AWS. The
 * aws-control reads are made to FAIL on purpose, one pane per frame:
 *
 *   01-files-list-error      Files pane: listing 502 → notice + Retry (was blank)
 *   02-usage-costs-error     Usage pane: bill read 502 → notice under the em-dash row
 *   03-backup-status-error   Backup pane: status 500 → notice (was a bare title)
 *   04-shares-list-error     Shares pane: ledger 500 → notice (was "no links"-shaped blank)
 *   05-accounts-error        page level: accounts 403 dashboard_owner_required → notice, not "disabled"
 *
 * Usage: node scripts/capture-aws-control-errors.mjs <devServerBase> [outDir] [lang] [theme]
 *   e.g. node scripts/capture-aws-control-errors.mjs http://127.0.0.1:6871 ../temp-screenshots/aws-control-error-notices
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE_URL = process.argv[2]
if (!BASE_URL) {
  console.error('usage: node scripts/capture-aws-control-errors.mjs <devServerBase> [outDir] [lang] [theme]')
  process.exit(2)
}
const OUT = process.argv[3] || '/tmp/aws-control-error-notices'
const LANG = process.argv[4] || 'en'
const THEME = process.argv[5] || 'dark'
mkdirSync(OUT, { recursive: true })

const ACC = '111122223333'
const B = '/api/apps/aws-control'
const nullSummary = { storage: null, sites: null, tasks: null, costMonthToDate: null }

const ACCOUNTS = {
  accounts: [{
    account: ACC, name: 'prod-main', health: 'ok', summary: nullSummary,
    profiles: [
      { name: 'prod-main', region: 'us-west-2', kind: 'sso', identityOk: true, account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`, detail: '', default: true },
    ],
  }],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
  generatedAt: '2026-09-03T22:00:00Z',
}
const GiB = 1024 ** 3
const DRIVE = {
  exists: true, bucket: `kirocrew-drive-${ACC}-usw2`, region: 'us-west-2',
  usage: {
    bytes: 3.2 * GiB, objects: 128,
    sections: {
      drive: { objects: 97, bytes: 1.9 * GiB },
      library: { objects: 23, bytes: 0.4 * GiB },
      backup: { objects: 8, bytes: 0.9 * GiB },
    },
  },
}
const CONSENT = (svc) => ({
  service: svc, serviceLabel: svc === 's3' ? 'Amazon S3' : 'AWS Cost Explorer',
  profile: 'prod-main', credentialSource: 'profile prod-main', region: 'us-west-2',
  account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`,
  identityResolved: true, identityDetail: '', granted: true, reason: '',
  revokedOnAccountChange: false,
  grant: { account: ACC, region: 'us-west-2', profile: 'prod-main', granted_at: '2026-08-20T09:00:00Z' },
})

/** The refusal shape the backend actually sends: prose for the agent, a code for the UI. */
const DENIED = { error: 'AccessDenied: User is not authorized to perform s3:ListBucket on kirocrew-drive-111122223333-usw2', code: 'aws_call_failed' }

/** Whether the accounts read itself fails — flipped for the last frame. */
let accountsFail = false

/** aws-control fixture router. Returns true when handled. */
const extra = async (path, route) => {
  const url = new URL(route.request().url())
  const p = url.pathname
  // The shared stub matches `**/api/**`, which on a Vite DEV server also catches
  // the app's own source modules under /src/api/. Those are the page, not the
  // gateway — let them through untouched.
  if (!p.startsWith('/api/')) return route.continue(), true
  if (p === `${B}/accounts`) {
    return accountsFail
      ? (json(route, { error: 'dashboard owner required', code: 'dashboard_owner_required' }, 403), true)
      : (json(route, ACCOUNTS), true)
  }
  if (p === '/api/aws/consent') return json(route, CONSENT(url.searchParams.get('service') || 's3')), true
  if (p === `${B}/profiles/available`) return json(route, { profiles: [], registeredCount: 1, max: 10, supported: true }), true
  if (p === `${B}/drive/${ACC}`) return json(route, DRIVE), true
  if (p === `${B}/drive/${ACC}/list`) return json(route, DENIED, 502), true
  if (p === `${B}/costs/${ACC}`) return json(route, { error: 'Cost Explorer is not enabled for this account', code: 'aws_call_failed' }, 502), true
  if (p === `${B}/backup/${ACC}`) return json(route, { error: 'backup ledger unreadable', code: 'http_500' }, 500), true
  if (p === `${B}/shares`) return json(route, { error: 'share ledger unreadable', code: 'http_500' }, 500), true
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
/** Every frame asserts its notice is on screen; a frame of blank space is what this replaces. */
const notice = async (testId) => {
  await page.getByTestId(testId).waitFor({ timeout: 20_000 })
  await page.getByTestId(testId).getByRole('button', { name: /ask the agent/i }).waitFor({ timeout: 5_000 })
}

// 1. Files: the listing fails.
await page.goto(`${BASE_URL}/aws-control/files`, { waitUntil: 'domcontentloaded' })
await notice('drive-list-error')
await page.getByTestId('drive-list-error-retry').waitFor()
await shot('01-files-list-error.png')

// 2. Usage: the bill read fails (the storage meter still renders — the drive read is fine).
await page.goto(`${BASE_URL}/aws-control/usage`, { waitUntil: 'domcontentloaded' })
await notice('costs-error')
await page.getByTestId('usage-storage').waitFor({ timeout: 15_000 })
await shot('02-usage-costs-error.png')

// 3. Backup: the status read fails.
await page.goto(`${BASE_URL}/aws-control/backup`, { waitUntil: 'domcontentloaded' })
await notice('backup-status-error')
await shot('03-backup-status-error.png')

// 4. Shares: the ledger read fails.
await page.goto(`${BASE_URL}/aws-control/shares`, { waitUntil: 'domcontentloaded' })
await notice('access-list-error')
await shot('04-shares-list-error.png')

// 5. Page level: a 403 that is NOT app_disabled is an error, not the disabled-app copy.
accountsFail = true
await page.goto(`${BASE_URL}/aws-control`, { waitUntil: 'domcontentloaded' })
await notice('aws-control-error')
await page.getByTestId('error-retry').waitFor()
await shot('05-accounts-error.png')

await browser.close()
console.log('done →', OUT)
