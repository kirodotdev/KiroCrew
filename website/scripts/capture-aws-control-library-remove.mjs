/**
 * Screenshot harness for the AWS Control Library's REMOVE control.
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call intercepted by Playwright and answered from
 * fixtures — no gateway, no dashboard token. Modelled on
 * capture-aws-control.mjs, which drives the same page down the same click path.
 *
 * Captures:
 *   library-remove-at-rest.png  the Library tiles at rest: a synced tile shows
 *                               Remove next to Push, an unsynced tile does not.
 *   library-remove-confirm.png  the inline confirm strip open on that tile
 *                               (Cancel + danger Remove).
 *
 * Usage: node scripts/capture-aws-control-library-remove.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/aws-control-library-remove'
mkdirSync(OUT, { recursive: true })

// ---- fixtures -------------------------------------------------------------
// One account is enough: the frames are about the Library tiles, not the list.
const ACCOUNTS = {
  supported: true,
  accounts: [
    {
      account: '217681647555', name: 'personal', health: 'ok',
      profiles: [{ name: 'personal', kind: 'credential-process', region: 'us-west-2', account: '217681647555', default: true, identityOk: true }],
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
  account: '217681647555',
  identityResolved: true,
  revokedOnAccountChange: false,
  // Must belong to the account the console renders, or the console shows no
  // receipt and mounts the orphan-consent rescue instead.
  grant: { account: '217681647555', region: 'us-west-2', profile: 'personal', granted_at: '2026-08-28T00:00:00+00:00' },
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

/**
 * Three tiles, chosen so the frame proves the gate and not just the button:
 * `pushedVersion !== null` is what reveals Remove, so a synced up-to-date tile
 * and a synced stale tile must both carry it while the never-synced tile must
 * not. A single synced fixture would screenshot identically whether the control
 * were gated on `synced` or hardcoded on.
 */
const LIBRARY = {
  artifacts: [
    { slug: 'release-notes', name: 'Release notes', kind: 'markdown', version: 4, updatedAt: '2026-08-27T10:15:00Z', pushedVersion: 4, pushedAt: '2026-08-27T10:20:00Z' },
    { slug: 'cost-dashboard', name: 'Cost dashboard', kind: 'widget', version: 7, updatedAt: '2026-08-29T08:02:00Z', pushedVersion: 5, pushedAt: '2026-08-26T21:44:00Z' },
    { slug: 'draft-onboarding', name: 'Draft onboarding', kind: 'html', version: 2, updatedAt: '2026-08-30T12:30:00Z', pushedVersion: null, pushedAt: null },
  ],
}

const BASE = '/api/apps/aws-control'
const BACKUP = { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }
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
  if (/^\/drive\/[^/]+\/list$/.test(app)) return json(route, { folders: [], files: [] })
  if (/^\/drive\/[^/]+$/.test(app)) return json(route, DRIVE)
  if (/^\/costs\/[^/]+$/.test(app)) return json(route, COSTS)
  if (app === '/profiles/available') return json(route, { supported: true, profiles: [], max: 20 })
  // The remove POST is the more specific route, so it must be tested BEFORE the
  // library list GET or the list regex would swallow it.
  if (/^\/library\/[^/]+\/remove$/.test(app)) return json(route, { removed: true })
  if (/^\/library\/[^/]+$/.test(app)) return json(route, LIBRARY)
  if (/^\/backup\/[^/]+$/.test(app)) return json(route, BACKUP)
  if (app.startsWith('/shares')) return json(route, { shares: [] })
  // ---- dashboard shell, not this app. The shell mounts BEFORE the app page and
  // several of these are consumed as ARRAYS, so a blanket {} crashes the shell's
  // error boundary and the app page never mounts at all.
  if (path === '/api/apps') return json(route, [])
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/status') return json(route, { sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, uptime: 1, version: '0.1.0' })
  if (path === '/api/kiro-prerequisite') return json(route, { installed: true, authenticated: true, ready: true })
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/chat/slots') return json(route, [])
  if (path === '/api/models') return json(route, { models: [], default: 'auto' })
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  const objectish = /(config|tips|voice|autonudge|branding|status|themes|system)/.test(path)
  unmatched.add(path)
  return json(route, objectish ? {} : [])
}

// ---- run ------------------------------------------------------------------
const { srv: server, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1180, height: 820 }, deviceScaleFactor: 2 })
await page.route('**/api/**', answer)
await page.route('**/api/ws', (route) => route.abort())
page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
  localStorage.setItem('mc-theme-mode', 'dark')
})

// Assertions must FAIL the run, not just print: a stale dist would exit 0 while
// every frame showed the pre-PR page with no Remove control at all.
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

// Accounts list → the account console → the drive → the Library section → the
// PICKER dialog. Same click path capture-aws-control.mjs walks; the Remove
// control lives on the picker's cards (PickerCard), not the folder listing, so
// the dialog must be open for either frame.
await page.goto(`${base}/aws-control`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1200)
if (await click('account-card')) await page.waitForTimeout(1200)
if (await click('capability-drive')) await page.waitForTimeout(900)
if (await click('drive-section-library')) await page.waitForTimeout(1200)
if (await click('library-add-open')) await page.waitForTimeout(900)

// ---- frame (a): picker cards at rest --------------------------------------
await expectCount('library-add-dialog', 1)
await expectCount('library-tile', 3)
// The gate itself: Remove on the two synced cards, absent on the unsynced one.
await expectCount('library-remove', 2)
await expectCount('library-remove-confirm', 0)
await page.screenshot({ path: `${OUT}/library-remove-at-rest.png`, fullPage: false })
console.log('shot library-remove-at-rest')

// ---- frame (b): the confirm strip open ------------------------------------
if (await click('library-remove')) await page.waitForTimeout(400)
// Opening the confirm swaps that tile's Remove trigger for the strip, so one
// trigger remains (the other synced tile) and the strip's two controls appear.
await expectCount('library-remove-confirm', 1)
await expectCount('library-remove-cancel', 1)
await expectCount('library-remove-action', 1)
await expectCount('library-remove', 1)
// The confirm names the artifact rather than asking a bare "are you sure?" —
// pinned here because a generic string screenshots just as plausibly.
const strip = await page.locator('[data-testid="library-remove-confirm"]').innerText().catch(() => '')
const named = strip.includes('Release notes')
console.log(`ASSERT confirm-names-artifact ${named ? 'ok' : 'MISMATCH: ' + JSON.stringify(strip.slice(0, 160))}`)
if (!named) failures.push('confirm strip does not name the artifact')
await page.screenshot({ path: `${OUT}/library-remove-confirm.png`, fullPage: false })
console.log('shot library-remove-confirm')

if (unmatched.size) console.log('unmatched /api paths:', [...unmatched].join(', '))
await browser.close()
server.close()
if (failures.length) {
  console.error('harness assertions failed (stale dist, or the UI changed):')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('done')
