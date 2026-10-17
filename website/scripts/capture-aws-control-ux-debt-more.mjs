/**
 * Supplementary frames for the aws-control UX-debt PR — the states the UX
 * Review lane asked to see on the branch itself. Same harness as
 * `capture-aws-control-ux-debt.mjs`: a Vite dev server with every /api/**
 * call answered from fixtures, no gateway, no credentials.
 *
 *   05-row-menu-open        the row menu on this branch, "Move to folder…" present
 *   06-move-in-flight       a move mid-copy: source row dimmed + aria-busy, picker says "Moving…",
 *                           and a second row's menu shows Move disabled
 *   07-move-refused         a 409 reported inside the picker
 *   08-accounts-empty       no accounts: empty state + Add accounts auto-expanded
 *   09-switcher-open        the account switcher open, "Needs attention" in words
 *   10-usage-consent        Usage pane: consent-missing cost reason inline; S3 receipt with credential source
 *   11-library-add-empty    Add-from-Artifacts picker on an empty library
 *   12-share-dialog-named   the Share dialog naming its file
 *
 * Usage: node capture-aws-control-ux-debt-more.mjs <devServerBase> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const [BASE_URL, OUT] = process.argv.slice(2)
if (!BASE_URL || !OUT) {
  console.error('usage: node capture-aws-control-ux-debt-more.mjs <devServerBase> <outDir>')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const ACC = '111122223333'
const ACC2 = '444455556666'
const B = '/api/apps/aws-control'
const GiB = 1024 ** 3
const nullSummary = { storage: null, sites: null, tasks: null, costMonthToDate: null }

const ACCOUNTS = {
  accounts: [
    {
      account: ACC, name: 'prod-main', health: 'ok', summary: { ...nullSummary, storage: 3.2 * GiB },
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
const NO_ACCOUNTS = { accounts: [], totals: { accounts: 0, profiles: 0, profilesHealthy: 0 }, generatedAt: '2026-09-05T08:00:00Z' }
const AVAILABLE = {
  supported: true, registeredCount: 0, max: 10,
  profiles: [
    { name: 'personal', region: 'us-east-1', kind: 'sso', registered: false },
    { name: 'sandbox', region: 'eu-central-1', kind: 'credential-process', registered: false },
  ],
}
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
/** Listings below the top level, keyed by the `path` the page asks for. */
const NESTED = {
  contracts: { files: [{ key: 'contracts/msa.pdf', size: 880_000, modified: '2026-08-12T09:00:00Z' }], folders: ['contracts/2026'] },
  'contracts/2026': {
    files: [{ key: 'contracts/2026/renewal.pdf', size: 410_000, modified: '2026-08-28T11:30:00Z' }],
    folders: ['contracts/2026/drafts'],
  },
}
const BACKUP = {
  nightly: true,
  runs: { snapshot: { key: 'backup/snapshot/2026-09-05.tar.gz', bytes: 0.6 * GiB, at: '2026-09-05T02:00:00Z' } },
  remote: {
    snapshot: [{ key: 'backup/snapshot/2026-09-05.tar.gz', size: 0.6 * GiB, modified: '2026-09-05T02:00:00Z' }],
    sessions: [],
  },
}
const CONSENT = (svc, granted) => ({
  service: svc, serviceLabel: svc === 's3' ? 'Amazon S3' : 'AWS Cost Explorer',
  profile: 'prod-main', credentialSource: 'profile prod-main (SSO)', region: 'us-west-2',
  account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Admin/dev`,
  identityResolved: true, identityDetail: '', granted, reason: '',
  revokedOnAccountChange: false,
  grant: granted ? { account: ACC, region: 'us-west-2', profile: 'prod-main', granted_at: '2026-08-20T09:00:00Z' } : null,
})
const COSTS_CONSENT_MISSING = {
  fresh: false, monthToDate: 0, projected: 0, currency: 'USD', byService: [], fetchedAt: '2026-09-05T08:00:00Z',
  consentMissing: true,
}

/** Frame-specific switches. */
const mode = { accountsEmpty: false, moveHangs: false, moveRefuses: false, noFolders: false, archiveDeleted: false, moveRefusesLate: false }

const extra = async (path, route) => {
  const url = new URL(route.request().url())
  const p = url.pathname
  if (!p.startsWith('/api/')) return route.continue(), true
  if (p === `${B}/accounts`) return json(route, mode.accountsEmpty ? NO_ACCOUNTS : ACCOUNTS), true
  if (p === `${B}/profiles/available`) return json(route, mode.accountsEmpty ? AVAILABLE : { ...AVAILABLE, registeredCount: 2, profiles: [] }), true
  if (p === '/api/aws/consent') {
    const svc = url.searchParams.get('service') || 's3'
    return json(route, CONSENT(svc, svc === 's3')), true
  }
  if (p === `${B}/drive/${ACC}`) return json(route, DRIVE), true
  if (p === `${B}/drive/${ACC}/list`) {
    const at = url.searchParams.get('path') || ''
    if (at) return json(route, NESTED[at] ?? { files: [], folders: [] }), true
    if (mode.noFolders) return json(route, { ...LISTING, folders: [] }), true
    if (mode.archiveDeleted) return json(route, { ...LISTING, folders: ['contracts'] }), true
    return json(route, LISTING), true
  }
  if (p === `${B}/drive/${ACC}/folder/delete`) {
    mode.archiveDeleted = true
    return json(route, { deleted: true, path: 'archive', objects: 14 }), true
  }
  if (p === `${B}/drive/${ACC}/move`) {
    if (mode.moveHangs) return new Promise(() => {}), true // never settles: the copy is "running"
    if (mode.moveRefuses) return json(route, { error: 'destination already holds this name', code: 'destination_exists' }, 409), true
    if (mode.moveRefusesLate) {
      await new Promise((r) => setTimeout(r, 1500)) // long enough for the reader to dismiss the picker first
      return json(route, { error: 'destination already holds this name', code: 'destination_exists' }, 409), true
    }
    return json(route, { moved: true }), true
  }
  if (p === `${B}/drive/${ACC}/search`) {
    return json(route, { results: [{ key: 'contracts/2026/renewal.pdf', size: 410_000, modified: '2026-08-28T11:30:00Z' }, { key: 'Q3-report.pdf', size: 2_480_000, modified: '2026-09-01T10:12:00Z' }], capped: false, limit: 200 }), true
  }
  if (p === `${B}/drive/${ACC}/share`) return new Promise(() => {}), true // mint never settles: "Creating…" holds
  if (p.startsWith(`${B}/costs/`)) return json(route, COSTS_CONSENT_MISSING), true
  if (p === `${B}/shares`) return json(route, { shares: [], checked: true }), true
  if (p === `${B}/library/${ACC}`) return json(route, { artifacts: [] }), true
  if (p === '/api/artifacts' || p.startsWith('/api/artifacts?')) return json(route, { artifacts: [] }), true
  if (p === `${B}/backup/${ACC}/restore`) return json(route, { downloaded: true, path: '~/.kiro/crew/restore/2026-09-05-snapshot/', bytes: 0.6 * GiB }), true
  if (p.startsWith(`${B}/backup/`)) return json(route, BACKUP), true
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

// 05. Row menu on this branch.
await files()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-move').waitFor()
await shot('05-row-menu-open-after')
await page.keyboard.press('Escape')

// 06. A move in flight: pick a folder while the copy never settles.
mode.moveHangs = true
await files()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-move').click()
await page.getByTestId('move-folder').first().click()
await page.getByTestId('move-pending').waitFor()
await page.getByTestId('drive-file').first().and(page.locator('[aria-busy="true"]')).waitFor()
await shot('06-move-in-flight-after')
// Dismiss the picker mid-move and open the other row's menu: Move is disabled.
await page.keyboard.press('Escape')
await page.getByTestId('move-dialog').waitFor({ state: 'detached' })
await page.getByTestId('drive-more').nth(1).click()
await page.getByTestId('drive-move').waitFor()
await shot('06b-move-disabled-while-busy-after')
await page.keyboard.press('Escape')
mode.moveHangs = false

// 07. A refused move, reported inside the picker.
mode.moveRefuses = true
await files()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-move').click()
await page.getByTestId('move-folder').first().click()
await page.getByTestId('move-error').waitFor()
await shot('07-move-refused-after')
await page.keyboard.press('Escape')
mode.moveRefuses = false

// 08. No accounts yet: the empty state, Add accounts already expanded.
mode.accountsEmpty = true
await page.goto(`${BASE_URL}/aws-control/accounts`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('accounts-empty').waitFor({ timeout: 20_000 })
await page.getByTestId('add-accounts-list').waitFor()
await shot('08-accounts-empty-after')
mode.accountsEmpty = false

// 09. The switcher open: the unhealthy account says so in words.
await page.goto(`${BASE_URL}/aws-control/accounts`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('accounts-list').waitFor({ timeout: 20_000 })
await page.getByTestId('account-switcher').click()
await page.getByTestId('switcher-option').first().waitFor()
await shot('09-switcher-open-after')
await page.keyboard.press('Escape')

// 10. Usage: consent-missing reason inline; S3 receipt shows its credential source.
await page.goto(`${BASE_URL}/aws-control/usage`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('console-cost-reason').waitFor({ timeout: 20_000 })
await shot('10-usage-consent-after')

// 11. Add from Artifacts on an EMPTY library.
await page.goto(`${BASE_URL}/aws-control/library`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('library-add-open').waitFor({ timeout: 20_000 })
await page.getByTestId('library-add-open').click()
await page.getByTestId('library-add-empty').waitFor()
await shot('11-library-add-empty-after')
await page.keyboard.press('Escape')

// 12. Share dialog names its file.
await files()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-share').click()
await page.getByTestId('share-dialog').waitFor()
await shot('12-share-dialog-named-after')
await page.keyboard.press('Escape')

// 13. Move picker opened two levels down: "Files (top level)", "Up to contracts", then the sub-folder.
await files()
await page.getByTestId('drive-folder-open').first().click() // contracts
await page.getByTestId('drive-folder-open').first().waitFor()
await page.getByTestId('drive-folder-open').first().click() // contracts/2026
await page.getByTestId('drive-file').first().waitFor()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-move').click()
await page.getByTestId('move-root').waitFor()
await page.getByTestId('move-up').waitFor()
await shot('13-move-picker-nested-after')
await page.keyboard.press('Escape')

// 14. Move picker at a top level with no folder at all.
mode.noFolders = true
await files()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-move').click()
await page.getByTestId('move-dialog').waitFor()
await page.getByText('There is no other folder here to move it into.', { exact: false }).waitFor()
await shot('14-move-no-folders-after')
await page.keyboard.press('Escape')
mode.noFolders = false

// 15. Picker dismissed mid-move: the dimmed row says "Moving…" in words.
mode.moveHangs = true
await files()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-move').click()
await page.getByTestId('move-folder').first().click()
await page.getByTestId('move-pending').waitFor()
await page.keyboard.press('Escape')
await page.getByTestId('move-dialog').waitFor({ state: 'detached' })
await page.getByTestId('drive-moving').waitFor()
await shot('15-move-row-moving-after')
mode.moveHangs = false

// 16. A folder deleted: the count line names how many objects went with it.
await files()
await page.getByTestId('drive-folder-more').nth(1).click() // archive
await page.getByTestId('drive-folder-delete').click()
await page.getByTestId('drive-folder-delete-action').click()
await page.getByTestId('drive-folder-deleted').waitFor()
await shot('16-folder-deleted-after')
mode.archiveDeleted = false

// 17. Backup restored: the note says current data was not replaced, and where the staging copy is.
await page.goto(`${BASE_URL}/aws-control/backup`, { waitUntil: 'domcontentloaded' })
await page.getByTestId('backup-remote-toggle').waitFor({ timeout: 20_000 })
await page.getByTestId('backup-remote-toggle').click()
await page.getByTestId('backup-restore').first().click()
await page.getByTestId('backup-restored').waitFor()
await shot('17-backup-restored-after')

// 18. Search results: the pinned header paints the page surface, like the folder table.
await files()
await page.getByTestId('drive-search-input').fill('report')
await page.getByTestId('drive-search-table').waitFor()
await shot('18-search-results-after')

// 19. A refusal that lands AFTER the picker was dismissed goes to the pane's strip.
mode.moveRefusesLate = true
await files()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-move').click()
await page.getByTestId('move-folder').first().click()
await page.getByTestId('move-pending').waitFor()
await page.keyboard.press('Escape')
await page.getByTestId('move-dialog').waitFor({ state: 'detached' })
await page.getByTestId('drive-move-error').waitFor({ timeout: 10_000 })
await shot('19-move-refused-on-strip-after')
mode.moveRefusesLate = false

// 20. Share dialog mid-mint: "Creating…", Close disabled, Escape held.
await files()
await page.getByTestId('drive-more').first().click()
await page.getByTestId('drive-share').click()
await page.getByTestId('share-create').waitFor()
await page.getByTestId('share-create').click()
await page.getByText('Creating…').waitFor()
await page.keyboard.press('Escape')
await page.waitForTimeout(300)
await page.getByTestId('share-dialog').waitFor()
await shot('20-share-mid-mint-after')

// 21. Breadcrumb ancestor menu open, two levels down.
await files()
await page.getByTestId('drive-folder-open').first().click() // contracts
await page.getByTestId('drive-folder-open').first().waitFor()
await page.getByTestId('drive-folder-open').first().click() // contracts/2026
await page.getByTestId('drive-crumb-more').waitFor()
await page.getByTestId('drive-crumb-more').click()
await page.getByTestId('drive-crumb-menu').waitFor()
await shot('21-crumb-menu-open-after')
await page.keyboard.press('Escape')

// 22. Grid view: the tile mid-move says "Moving…"; its neighbour's Move item is disabled.
mode.moveHangs = true
await files()
await page.getByRole('button', { name: 'Grid view' }).click()
await page.getByTestId('drive-grid').waitFor()
await page.getByTestId('drive-grid-more').first().click()
await page.getByTestId('drive-grid-move').click()
await page.getByTestId('move-folder').first().click()
await page.getByTestId('move-pending').waitFor()
await page.keyboard.press('Escape')
await page.getByTestId('move-dialog').waitFor({ state: 'detached' })
await page.getByTestId('drive-moving').waitFor()
await page.getByTestId('drive-grid-more').nth(1).click()
await page.getByTestId('drive-grid-move').waitFor()
await shot('22-grid-moving-after')
await page.keyboard.press('Escape')
mode.moveHangs = false

await browser.close()
console.log('done →', OUT)
