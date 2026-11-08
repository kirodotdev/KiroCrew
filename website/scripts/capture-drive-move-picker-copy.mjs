/**
 * Frames for the Drive Move picker's next-step copy. Same harness shape as
 * `capture-aws-control-ux-debt-more.mjs`: a Vite dev server with every /api/**
 * call answered from fixtures, no gateway, no credentials. Run it once against
 * a main checkout and once against the branch; a step that only exists on the
 * branch is skipped (and logged) on main rather than failing the run.
 *
 *   01-move-no-folders        the picker with nowhere to move to (+ New folder on the branch)
 *   02-move-new-folder        after New folder: picker gone, folder-name field focused (branch only)
 *   03-move-in-flight         the picker mid-move: its progress line
 *   04-move-disabled-busy     another row's menu while the move runs: Move disabled, with its reason
 *   05-move-refused           a name clash inside the picker: the sentence and the hand-off
 *
 * Usage: node capture-drive-move-picker-copy.mjs <devServerBase> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const [BASE_URL, OUT] = process.argv.slice(2)
if (!BASE_URL || !OUT) {
  console.error('usage: node capture-drive-move-picker-copy.mjs <devServerBase> <outDir>')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const ACC = '111122223333'
const B = '/api/apps/aws-control'
const GiB = 1024 ** 3
const ACCOUNTS = {
  accounts: [{
    account: ACC, name: 'prod-main', health: 'ok',
    summary: { storage: 3.2 * GiB, sites: null, tasks: null, costMonthToDate: null },
    profiles: [{ name: 'prod-main', region: 'us-west-2', kind: 'sso', identityOk: true, account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`, detail: '', default: true }],
  }],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
  generatedAt: '2026-10-29T08:00:00Z',
}
const DRIVE = {
  exists: true, bucket: `kirocrew-drive-${ACC}-usw2`, region: 'us-west-2',
  usage: { bytes: 3.2 * GiB, objects: 128, sections: {
    drive: { objects: 97, bytes: 1.9 * GiB }, library: { objects: 23, bytes: 0.4 * GiB }, backup: { objects: 8, bytes: 0.9 * GiB },
  } },
}
const LISTING = {
  files: [
    { key: 'Q3-report.pdf', size: 2_480_000, modified: '2026-10-01T10:12:00Z' },
    { key: 'launch-notes.md', size: 12_400, modified: '2026-09-30T16:40:00Z' },
  ],
  folders: ['contracts', 'archive'],
}
const CONSENT = (svc) => ({
  service: svc, serviceLabel: svc === 's3' ? 'Amazon S3' : 'AWS Cost Explorer',
  profile: 'prod-main', credentialSource: 'profile prod-main (SSO)', region: 'us-west-2',
  account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`,
  identityResolved: true, identityDetail: '', granted: svc === 's3', reason: '',
  revokedOnAccountChange: false,
  grant: svc === 's3' ? { account: ACC, region: 'us-west-2', profile: 'prod-main', granted_at: '2026-08-20T09:00:00Z' } : null,
})

const mode = { noFolders: false, moveHangs: false, moveRefuses: false }

const extra = async (path, route) => {
  const url = new URL(route.request().url())
  const p = url.pathname
  if (!p.startsWith('/api/')) return route.continue(), true
  if (p === `${B}/accounts`) return json(route, ACCOUNTS), true
  if (p === `${B}/profiles/available`) return json(route, { supported: true, registeredCount: 1, max: 10, profiles: [] }), true
  if (p === '/api/aws/consent') return json(route, CONSENT(url.searchParams.get('service') || 's3')), true
  if (p === `${B}/drive/${ACC}`) return json(route, DRIVE), true
  if (p === `${B}/drive/${ACC}/list`) {
    if (url.searchParams.get('path')) return json(route, { files: [], folders: [] }), true
    return json(route, mode.noFolders ? { ...LISTING, folders: [] } : LISTING), true
  }
  if (p === `${B}/drive/${ACC}/move`) {
    if (mode.moveHangs) return new Promise(() => {}), true // never settles: the copy is "running"
    if (mode.moveRefuses) return json(route, { error: 'destination already holds this name', code: 'destination_exists' }, 409), true
    return json(route, { moved: true }), true
  }
  if (p.startsWith(`${B}/costs/`)) return json(route, { fresh: false, monthToDate: 0, projected: 0, currency: 'USD', byService: [], fetchedAt: '2026-10-29T08:00:00Z', consentMissing: true }), true
  if (p === `${B}/shares`) return json(route, { shares: [], checked: true }), true
  if (p === `${B}/library/${ACC}`) return json(route, { artifacts: [] }), true
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
  await page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log('captured', `${name}.png`)
}
const files = async () => {
  await page.goto(`${BASE_URL}/aws-control/files`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('drive-file').first().waitFor({ timeout: 20_000 })
}
const openPicker = async (row = 0) => {
  await page.getByTestId('drive-more').nth(row).click()
  await page.getByTestId('drive-move').click()
  await page.getByTestId('move-dialog').waitFor()
}

// 01 + 02. Nowhere to move to.
mode.noFolders = true
await files()
await openPicker()
await page.getByTestId('move-no-folders').waitFor()
await shot('01-move-no-folders')
if (await page.getByTestId('move-new-folder').count()) {
  await page.getByTestId('move-new-folder').click()
  await page.getByTestId('move-dialog').waitFor({ state: 'detached' })
  await page.getByTestId('drive-folder-name').waitFor()
  const focused = await page.evaluate(() => document.activeElement?.getAttribute('data-testid'))
  console.log('ASSERT focus after New folder:', focused)
  if (focused !== 'drive-folder-name') { console.error('focus did not land on the folder-name field'); process.exit(1) }
  await shot('02-move-new-folder')
} else {
  console.log('skip 02: no New folder action in this build')
  await page.keyboard.press('Escape')
}
mode.noFolders = false

// 03 + 04. A move in flight.
mode.moveHangs = true
await files()
await openPicker()
await page.getByTestId('move-folder').first().click()
await page.getByTestId('move-pending').waitFor()
await shot('03-move-in-flight')
await page.keyboard.press('Escape')
await page.getByTestId('move-dialog').waitFor({ state: 'detached' })
await page.getByTestId('drive-more').nth(1).click()
await page.getByTestId('drive-move').waitFor()
await shot('04-move-disabled-busy')
await page.keyboard.press('Escape')
mode.moveHangs = false

// 05. A name clash, reported in the picker.
mode.moveRefuses = true
await files()
await openPicker()
await page.getByTestId('move-folder').first().click()
await page.getByTestId('move-error').waitFor()
await shot('05-move-refused')
mode.moveRefuses = false

await browser.close()
console.log('done →', OUT)
