/**
 * Demo recorder for issue #9472 -- the "Add accounts" profile filter.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered from fixtures, so there is no gateway, no token
 * and no dependency on the recording machine's own ~/.aws/config. Modelled on
 * scripts/capture-aws-control.mjs; the only material difference is the
 * /profiles/available fixture, which carries 50 profiles in prefix-sharing
 * families -- the shape the issue reports as unusable without a filter.
 *
 * Records one webm; convert to mp4 outside this script.
 *
 * Usage: node scripts/record-add-accounts-filter.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, json, logPageFailures } from './lib/api-fixtures.mjs'

const OUT = process.argv[2] || '/tmp/add-accounts-filter'
mkdirSync(OUT, { recursive: true })

const BASE = '/api/apps/aws-control'

// ---- fixtures -------------------------------------------------------------
const ACCOUNTS = {
  supported: true,
  accounts: [
    {
      account: '217681647555', name: 'personal', health: 'ok',
      profiles: [{ name: 'personal', kind: 'credential-process', region: 'us-west-2', account: '217681647555', default: true, identityOk: true }],
    },
    {
      account: '740412361337', name: 'wombats-alpha', health: 'ok',
      profiles: [{ name: 'wombats-alpha-admin', kind: 'sso', region: 'us-west-2', account: '740412361337', default: false, identityOk: true }],
    },
    {
      account: '000417292745', name: 'beetlejuice-auth-syd', health: 'ok',
      profiles: [{ name: 'beetlejuice-syd', kind: 'sso', region: 'ap-southeast-2', account: '000417292745', default: false, identityOk: true }],
    },
  ],
  totals: { accounts: 3, profiles: 3, profilesHealthy: 3 },
}

/**
 * 3 registered + 47 unregistered = the issue's own "3 of 50 registered".
 *
 * The unregistered names come in families that share a long prefix and differ
 * in a short middle segment, because that is the case the issue names: dozens
 * of siblings out of a provisioning tool, indistinguishable at a glance.
 */
function buildProfiles() {
  const out = [
    { name: 'personal', registered: true },
    { name: 'wombats-alpha-admin', registered: true },
    { name: 'beetlejuice-syd', registered: true },
  ]
  const REGIONS = ['us-east-1', 'us-west-2', 'eu-west-1', 'eu-central-1', 'ap-southeast-2', 'ap-northeast-1']
  for (const stage of ['prod', 'staging', 'dev']) {
    for (const region of REGIONS) {
      out.push({ name: `acme-platform-${stage}-${region}`, registered: false })
    }
  }
  for (const team of ['orion', 'lyra', 'vega', 'draco']) {
    for (const role of ['admin', 'readonly', 'deploy', 'break-glass']) {
      out.push({ name: `orbital-${team}-${role}`, registered: false })
    }
  }
  for (const n of ['sandbox', 'sandbox-scratch', 'legacy-billing', 'legacy-audit-archive',
    'partner-integration-a', 'partner-integration-b', 'training-lab-01',
    'training-lab-02', 'training-lab-03', 'ops-oncall-bridge',
    'ops-oncall-shadow', 'security-review', 'security-review-ro']) {
    out.push({ name: n, registered: false })
  }
  return out
}

const PROFILES = buildProfiles()
const AVAILABLE = {
  supported: true,
  profiles: PROFILES,
  registeredCount: 3,
  max: 50,
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
  grant: { account: '217681647555', region: 'us-west-2', profile: 'personal', granted_at: '2026-09-01T00:00:00+00:00' },
})

const DRIVE = {
  account: '217681647555', region: 'us-west-2', bucket: '', provisioned: false,
  sections: [], totals: { objects: 0, bytes: 0 },
}
const LISTING = { entries: [], prefix: '', truncated: false }
const COSTS = { monthToDate: 2.25, currency: 'USD', fetchedAt: new Date().toISOString(), fresh: true, consentMissing: false }

/**
 * This app's own routes. Everything else -- the dashboard boot path the SPA
 * needs before it renders anything -- falls through to lib/api-fixtures.mjs,
 * which exists so two harnesses do not carry byte-identical copies of that
 * table (`npm run jscpd` runs at a 0% threshold and fails on the clone).
 */
async function answer(route) {
  const url = new URL(route.request().url())
  const path = url.pathname
  if (path.endsWith('/accounts')) return json(route, ACCOUNTS)
  if (path === '/api/aws/consent') return json(route, CONSENT(url.searchParams.get('service') || 's3'))
  const app = path.startsWith(BASE) ? path.slice(BASE.length) : ''
  if (app === '/profiles/available') return json(route, AVAILABLE)
  if (/^\/drive\/[^/]+\/list$/.test(app)) return json(route, LISTING)
  if (/^\/drive\/[^/]+$/.test(app)) return json(route, DRIVE)
  if (/^\/costs\/[^/]+$/.test(app)) return json(route, COSTS)
  if (/^\/library\/[^/]+$/.test(app)) return json(route, { artifacts: [] })
  if (/^\/backup\/[^/]+$/.test(app)) return json(route, { jobs: {}, local: {}, remote: null })
  if (app.startsWith('/shares')) return json(route, { shares: [] })
  return route.fallback()
}

// ---- run ------------------------------------------------------------------
const { srv: server, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  recordVideo: { dir: OUT, size: { width: 1280, height: 800 } },
})
const page = await context.newPage()
// Playwright matches handlers in reverse registration order, so the shared
// boot-path table goes on FIRST and this app's router, registered after it,
// runs first and calls route.fallback() for everything it does not own.
await installApiFixtures(page)
await page.route('**/api/**', answer)
logPageFailures(page)
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
  localStorage.setItem('mc-theme-mode', 'dark')
})

const failures = []
const expectCount = async (t, want) => {
  const got = await page.locator(`[data-testid="${t}"]`).count()
  const ok = got === want
  console.log(`ASSERT ${t} want=${want} got=${got} ${ok ? 'ok' : 'MISMATCH'}`)
  if (!ok) failures.push(`${t}: want ${want}, got ${got}`)
}
const rows = () => page.locator('[data-testid="add-accounts-checkbox"]')
const expectRows = async (want) => {
  const got = await rows().count()
  const ok = got === want
  console.log(`ASSERT rows want=${want} got=${got} ${ok ? 'ok' : 'MISMATCH'}`)
  if (!ok) failures.push(`rows: want ${want}, got ${got}`)
}
/** Type like a person so the narrowing is legible frame by frame. */
const typeFilter = async (text, { clear = true } = {}) => {
  const box = page.locator('[data-testid="add-accounts-search"]')
  if (clear) await box.fill('')
  await box.click()
  // A click lands the caret wherever it lands. Appending has to start from the
  // end or the typed run is spliced into the middle of the existing query.
  await box.press('End')
  await box.pressSequentially(text, { delay: 110 })
  await page.waitForTimeout(700)
}

await page.goto(`${base}/aws-control`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1400)

// 1. the Accounts & credentials pane
await page.locator('[data-testid="rail-accounts"]').click()
await page.waitForTimeout(1100)
await expectCount('accounts-pane', 1)
await expectCount('add-accounts', 1)

// 2. open the picker: 47 unregistered profiles, and now a search box
await page.locator('[data-testid="add-accounts-toggle"]').click()
await page.waitForTimeout(900)
await expectCount('add-accounts-search', 1)
await expectRows(47)
await page.locator('[data-testid="add-accounts"]').evaluate((el) =>
  el.scrollIntoView({ block: 'start', behavior: 'smooth' }),
)
await page.waitForTimeout(1500)
await page.screenshot({ path: `${OUT}/01-unfiltered.png` })

// 3. narrow to one family
await typeFilter('acme-platform-prod')
await expectRows(6)
await page.waitForTimeout(900)
await page.screenshot({ path: `${OUT}/02-filtered.png` })

// 4. narrow to one profile
await typeFilter('-eu-west-1', { clear: false })
await expectRows(1)
await page.waitForTimeout(1100)

// 5. nothing matches: the filter says it hid them, never "none left to add" --
//    which would assert every local profile is already registered.
await typeFilter('zzq')
await expectCount('add-accounts-search-empty', 1)
await expectCount('add-accounts-none', 0)
await page.waitForTimeout(1600)
await page.screenshot({ path: `${OUT}/03-no-match.png` })

// 6. Clear filter puts the whole list back
await page.locator('[data-testid="add-accounts-search-empty-clear"]').click()
await page.waitForTimeout(900)
await expectRows(47)
await page.waitForTimeout(1000)

// 7. tick one profile, then filter to a family it does not belong to. The tick
//    stays on screen and the list says why: Register acts on the tick set, so a
//    tick out of sight would register a profile the operator never saw.
await typeFilter('acme-platform-prod-eu-west-1')
await expectRows(1)
await rows().first().click()
await page.waitForTimeout(1100)
await typeFilter('orbital-lyra')
await expectRows(5)
await expectCount('add-accounts-kept-selected', 1)
await page.waitForTimeout(2100)
await page.screenshot({ path: `${OUT}/04-selection-kept.png` })

await context.close()
await browser.close()
server.close()

if (failures.length) {
  console.log(`\nFAILED (${failures.length}):`)
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}
console.log(`\nOK -- video in ${OUT}`)
