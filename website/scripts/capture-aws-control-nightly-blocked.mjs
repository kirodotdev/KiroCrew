/**
 * UX evidence for PR #13259: the nightly-SNAPSHOT "blocked" notice under the
 * "Back up every night" switch, in its two code-mapped wordings. Runs against a
 * Vite dev server with every /api/** call answered from fixtures (no gateway, no
 * AWS). The backup status is stubbed with the switch ON and `nightlyBlocked` set
 * to each stable code, so the console renders the localized sentence the console
 * maps the code to:
 *   01-nightly-blocked-mask-off   snapshot_mask_off          -> "paused, re-enable the sandbox"
 *   02-nightly-blocked-host       snapshot_host_unsupported  -> "can't run on this computer"
 *
 * Usage: node scripts/capture-aws-control-nightly-blocked.mjs <devServerBase> [outDir] [lang] [theme]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'
import { ACC, B, ACCOUNTS, DRIVE, CONSENT } from './lib/aws-control-fixtures.mjs'

const BASE_URL = process.argv[2]
if (!BASE_URL) {
  console.error('usage: node scripts/capture-aws-control-nightly-blocked.mjs <devServerBase> [outDir] [lang] [theme]')
  process.exit(2)
}
const OUT = process.argv[3] || '/tmp/aws-control-nightly-blocked'
const LANG = process.argv[4] || 'en'
const THEME = process.argv[5] || 'dark'
mkdirSync(OUT, { recursive: true })

/** The grant is ON and the snapshot nightly is blocked; `nightlyBlocked` is the CODE. */
let blockedCode = 'snapshot_mask_off'
const BACKUP = () => ({
  nightly: true,
  nightlyBlocked: blockedCode,
  nightlySessions: false,
  nightlySessionsBlocked: null,
  runs: {},
  jobs: {},
  install: { id: 'install-abc', label: 'This computer' },
  remote: { snapshot: [], sessions: [], installs: [], others: 0, truncated: false, max: 8 },
})

const extra = async (path, route) => {
  const url = new URL(route.request().url())
  const p = url.pathname
  if (!p.startsWith('/api/')) return route.continue(), true
  if (p === `${B}/accounts`) return json(route, ACCOUNTS), true
  if (p === '/api/aws/consent') return json(route, CONSENT(url.searchParams.get('service') || 's3')), true
  if (p === `${B}/profiles/available`) return json(route, { profiles: [], registeredCount: 1, max: 10, supported: true }), true
  if (p === `${B}/drive/${ACC}`) return json(route, DRIVE), true
  if (p === `${B}/drive/${ACC}/list`) return json(route, { entries: [] }), true
  if (p === `${B}/backup/${ACC}`) return json(route, BACKUP()), true
  if (p === `${B}/library/${ACC}`) return json(route, { artifacts: [] }), true
  if (p === `${B}/shares`) return json(route, { shares: [] }), true
  return false
}

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
logPageProblems(page)
await stubDashboardApi(page, { slots: [], theme: THEME, localStorageEntries: { 'mc-lang': LANG }, extra })

const shot = async (name) => {
  const row = page.getByTestId('backup-nightly')
  await row.waitFor({ timeout: 20_000 })
  const blocked = page.getByTestId('backup-nightly-blocked')
  await blocked.waitFor({ timeout: 20_000 })
  await page.waitForTimeout(300)
  const section = page.getByTestId('backup-nightly').locator('xpath=ancestor::*[self::section or self::div][1]')
  await (await section.count() ? section.first() : page).screenshot({ path: join(OUT, name) })
  console.log('captured', name, '->', (await blocked.textContent() || '').trim())
}

blockedCode = 'snapshot_mask_off'
await page.goto(`${BASE_URL}/aws-control/backup`, { waitUntil: 'domcontentloaded' })
await shot('01-nightly-blocked-mask-off.png')

blockedCode = 'snapshot_host_unsupported'
await page.goto(`${BASE_URL}/aws-control/backup?r=2`, { waitUntil: 'domcontentloaded' })
await page.reload({ waitUntil: 'domcontentloaded' })
await shot('02-nightly-blocked-host.png')

await browser.close()
console.log('done ->', OUT)
