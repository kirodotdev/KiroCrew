/**
 * Before/after frames for the aws-control UX-debt PR. Runs against a Vite dev
 * server with every /api/** call answered from fixtures — no gateway, no
 * credentials, no real AWS. The same script runs on main (before) and on the
 * branch (after); frames whose control only exists after (Move to folder…) fall
 * back to the open row menu so the "before" shows what the reader had instead.
 *
 * Usage: node capture-aws-control-ux-debt.mjs <devServerBase> <outDir> <tag>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const [BASE_URL, OUT, TAG = 'after'] = process.argv.slice(2)
if (!BASE_URL || !OUT) {
  console.error('usage: node capture-aws-control-ux-debt.mjs <devServerBase> <outDir> <before|after>')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const ACC = '111122223333'
const ACC2 = '444455556666'
const B = '/api/apps/aws-control'
const nullSummary = { storage: null, sites: null, tasks: null, costMonthToDate: null }

const ACCOUNTS = {
  accounts: [
    {
      account: ACC, name: 'prod-main', health: 'ok', summary: { ...nullSummary, storage: 3.2 * 1024 ** 3 },
      profiles: [
        { name: 'prod-main', region: 'us-west-2', kind: 'sso', identityOk: true, account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`, detail: '', default: true },
        { name: 'prod-readonly', region: 'us-west-2', kind: 'sso', identityOk: true, account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/ReadOnly/dev`, detail: '', default: false },
      ],
    },
    {
      account: ACC2, name: 'staging', health: 'degraded', summary: nullSummary,
      profiles: [
        { name: 'staging', region: 'eu-west-1', kind: 'credential-process', identityOk: false, account: ACC2, arn: '', detail: 'The SSO session has expired.', default: true },
      ],
    },
  ],
  totals: { accounts: 2, profiles: 3, profilesHealthy: 2 },
  generatedAt: '2026-09-05T08:00:00Z',
}
const GiB = 1024 ** 3
const DRIVE = {
  exists: true, bucket: `kirocrew-drive-${ACC}-usw2`, region: 'us-west-2',
  usage: { bytes: 3.2 * GiB, objects: 128, sections: {
    drive: { objects: 97, bytes: 1.9 * GiB }, library: { objects: 23, bytes: 0.4 * GiB }, backup: { objects: 8, bytes: 0.9 * GiB },
  } },
}
const LISTING = {
  files: [
    { key: 'Q3-report.pdf', size: 2_480_000, modified: '2026-09-01T10:12:00Z' },
    { key: 'launch-notes.md', size: 12_400, modified: '2026-08-30T16:40:00Z' },
  ],
  folders: ['contracts', 'archive'],
}
const SHARES = {
  checked: true,
  shares: [
    { id: 's1', account: ACC, section: 'drive', key: 'Q3-report.pdf', createdAt: '2026-09-04T09:00:00Z', expiresAt: '2026-09-11T09:00:00Z', note: 'for the Q3 review' },
    { id: 's2', account: ACC, section: 'library', key: 'artifacts/status-dashboard.html', createdAt: '2026-09-03T12:00:00Z', expiresAt: '2026-09-05T12:00:00Z', note: '' },
  ],
}
const CONSENT = (svc) => ({
  service: svc, serviceLabel: svc === 's3' ? 'Amazon S3' : 'AWS Cost Explorer',
  profile: 'prod-main', credentialSource: 'profile prod-main', region: 'us-west-2',
  account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`,
  identityResolved: true, identityDetail: '', granted: true, reason: '',
  revokedOnAccountChange: false,
  grant: { account: ACC, region: 'us-west-2', profile: 'prod-main', granted_at: '2026-08-20T09:00:00Z' },
})

const extra = async (path, route) => {
  const url = new URL(route.request().url())
  const p = url.pathname
  if (!p.startsWith('/api/')) return route.continue(), true
  if (p === `${B}/accounts`) return json(route, ACCOUNTS), true
  if (p === '/api/aws/consent') return json(route, CONSENT(url.searchParams.get('service') || 's3')), true
  if (p === `${B}/profiles/available`) return json(route, { profiles: [], registeredCount: 2, max: 10, supported: true }), true
  if (p === `${B}/drive/${ACC}`) return json(route, DRIVE), true
  if (p === `${B}/drive/${ACC}/list`) return json(route, LISTING), true
  if (p === `${B}/shares`) return json(route, SHARES), true
  if (p === `${B}/library/${ACC}`) return json(route, { artifacts: [] }), true
  if (p.startsWith(`${B}/costs/`)) return json(route, { error: 'Cost Explorer is not enabled for this account', code: 'aws_call_failed' }, 502), true
  if (p.startsWith(`${B}/backup/`)) return json(route, { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }), true
  return false
}

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1 })
const page = await ctx.newPage()
logPageProblems(page)
await stubDashboardApi(page, { slots: [], theme: 'dark', localStorageEntries: { 'mc-lang': 'en' }, extra })

const shot = async (name) => {
  await page.waitForTimeout(500)
  await page.screenshot({ path: join(OUT, `${name}-${TAG}.png`) })
  console.log('captured', `${name}-${TAG}.png`)
}
const has = async (testId, timeout = 4000) =>
  page.getByTestId(testId).first().waitFor({ timeout }).then(() => true, () => false)

// 1. Accounts pane: rail + totals strip + the selected account's keys section.
await page.goto(`${BASE_URL}/aws-control/accounts`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('accounts-list').waitFor({ timeout: 20_000 })
await page.getByTestId('accounts-totals').waitFor({ timeout: 10_000 })
await shot('01-accounts-rail-summary')

// 2. Share links pane (was "Access").
await page.goto(`${BASE_URL}/aws-control/shares`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('access-row').first().waitFor({ timeout: 20_000 })
await shot('02-share-links')

// 3. Files: row menu → Move to folder… picker (after) / the row menu as it was (before).
await page.goto(`${BASE_URL}/aws-control/files`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('drive-file').first().waitFor({ timeout: 20_000 })
await page.getByTestId('drive-more').first().click()
if (await has('drive-move')) {
  await page.getByTestId('drive-move').click()
  await page.getByTestId('move-dialog').waitFor({ timeout: 5000 })
}
await shot('03-move-to-folder')

// 4. Files: row menu → Share dialog.
await page.goto(`${BASE_URL}/aws-control/files`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('drive-file').first().waitFor({ timeout: 20_000 })
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-share').click()
await page.getByTestId('share-dialog').waitFor({ timeout: 5000 })
await shot('04-share-dialog')

await browser.close()
console.log('done →', OUT)
