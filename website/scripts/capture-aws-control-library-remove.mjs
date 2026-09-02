/**
 * Screenshot harness for the AWS Control Library's REMOVE control.
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call intercepted by Playwright and answered from
 * fixtures — no gateway, no dashboard token. Modelled on
 * capture-aws-control.mjs, which drives the same page down the same click path.
 *
 * The fixtures are the reason this is not a pod: the frames need a bucket that
 * already HOLDS cloud copies, one of them pushed from a machine that is not this
 * one. A pod has no AWS profile, no S3 consent and no bucket, so its Library
 * folder is empty and the control under test is unreachable. The bundle served
 * here is the same `website/dist` a pod provisions; only the JSON is faked.
 *
 * Captures the FULL app window (viewport, not a crop) at deviceScaleFactor 2:
 *   library-remove-at-rest.png  the Library folder's listing of what is really
 *                               in the bucket: one card per object, each with
 *                               the same `⋮` the Files folder's cards use, and
 *                               no destructive control on display.
 *   library-remove-menu.png     that menu OPEN, which is the only frame in which
 *                               a deliberately hidden control can be seen.
 *   library-remove-confirm.png  the inline confirm the item opens, naming the
 *                               item AND the artifacts/<slug>/ prefix it empties.
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
    { slug: 'cost-dashboard', name: 'Cost dashboard', kind: 'svg', version: 7, updatedAt: '2026-08-29T08:02:00Z', pushedVersion: 5, pushedAt: '2026-08-26T21:44:00Z' },
    { slug: 'draft-onboarding', name: 'Draft onboarding', kind: 'html', version: 2, updatedAt: '2026-08-30T12:30:00Z', pushedVersion: null, pushedAt: null },
  ],
}

/**
 * What is actually in the bucket, which is what the folder lists and what the
 * removal targets. Chosen so the frame proves the placement rather than just the
 * control:
 *
 *   release-notes         a copy whose local artifact matches -- the ordinary case;
 *   cost-dashboard        pushed at v5 while the local copy moved to v7, so the
 *                         stale-version disclosure sits beside a live removal;
 *   from-another-machine  NO local artifact at all. This is the case the picker
 *                         placement could not reach: it walks the local store, so
 *                         a copy pushed elsewhere had no row to carry a control.
 *
 * `draft-onboarding` is deliberately absent: it was never pushed, so nothing of
 * it is in the bucket and no card of it may appear in this folder.
 */
const CLOUD_FOLDERS = ['cost-dashboard', 'from-another-machine', 'release-notes']

/**
 * The artifact bodies the CARD PREVIEWS render.
 *
 * `ArtifactPreview` fetches the full artifact on the shared `['artifact', slug]`
 * key, so without these the previews stay the reserved grey box and the frame
 * shows cards that look half-loaded. Only the pushed slugs need one --
 * `from-another-machine` has no local artifact by construction and draws the
 * cloud-only thumb instead.
 */
const ARTIFACT_BODIES = {
  'release-notes': {
    slug: 'release-notes', name: 'Release notes', kind: 'markdown', version: 4,
    updatedAt: '2026-08-27T10:15:00Z',
    // Short on purpose: ContentThumb gives markdown up to 300px, and a long body
    // makes this card tower over its neighbours in a frame meant to show three
    // cards carrying the same control.
    content: '## Cloud drive\n\n- Copies are removable from the folder that lists them.\n- A copy pushed elsewhere is reachable like any other.\n',
  },
  'cost-dashboard': {
    slug: 'cost-dashboard', name: 'Cost dashboard', kind: 'svg', version: 7,
    updatedAt: '2026-08-29T08:02:00Z',
    // svg rather than widget: ContentThumb draws an svg INLINE, while WidgetThumb
    // renders through a sandboxed srcdoc iframe that paints nothing headless, so
    // the preview photographs as an empty panel and the card reads as broken UI.
    // Still a DRAWN artifact rather than a second block of prose, so the frame
    // keeps two different kinds side by side.
    content: [
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 150" width="300" height="150">',
      '<rect width="300" height="150" rx="10" fill="#1e1b2e" stroke="#322c4d"/>',
      '<text x="18" y="34" fill="#a79fd0" font-family="system-ui" font-size="10" letter-spacing="1.4">MONTH TO DATE</text>',
      '<text x="18" y="62" fill="#ece9f7" font-family="system-ui" font-size="24" font-weight="600">$2.25</text>',
      [34, 52, 41, 68, 47, 73, 58].map((h, i) =>
        `<rect x="${18 + i * 38}" y="${130 - h * 0.62}" width="26" height="${h * 0.62}" rx="3" fill="#8b7cf6"/>`).join(''),
      '</svg>',
    ].join(''),
  },
}

const BASE = '/api/apps/aws-control'
const BACKUP = { nightly: false, runs: {}, remote: { snapshot: [], sessions: [] } }
const unmatched = new Set()
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

async function answer(route) {
  const path = new URL(route.request().url()).pathname
  if (path.endsWith('/accounts')) return json(route, ACCOUNTS)
  // The card previews' shared artifact key. Matched before the app routes since
  // it is a dashboard path, not an app-scoped one.
  const art = /^\/api\/artifacts\/([^/]+)$/.exec(path)
  if (art) {
    const body = ARTIFACT_BODIES[decodeURIComponent(art[1])]
    return body ? json(route, body) : route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  }
  if (path === '/api/aws/consent') {
    const svc = new URL(route.request().url()).searchParams.get('service') || 's3'
    return json(route, CONSENT(svc))
  }
  // App paths are BASE-prefixed and account-scoped, so match on the segment
  // after the base rather than on a suffix.
  const app = path.startsWith(BASE) ? path.slice(BASE.length) : ''
  if (/^\/drive\/[^/]+\/list$/.test(app)) {
    const section = new URL(route.request().url()).searchParams.get('section')
    // Only the LIBRARY section holds these; Files must stay empty or the frame
    // would show the same names under two folders that do not share objects.
    return json(route, { folders: section === 'library' ? CLOUD_FOLDERS : [], files: [] })
  }
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

// Accounts list → the account console → the drive → the Library section. The
// control lives on the FOLDER's listing of what is really in the bucket, so no
// dialog is opened: the picker deliberately has no removal to photograph.
await page.goto(`${base}/aws-control`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1200)
if (await click('account-card')) await page.waitForTimeout(1200)
if (await click('capability-drive')) await page.waitForTimeout(900)
if (await click('drive-section-library')) await page.waitForTimeout(2600)

// ---- frame (a): the cloud listing at rest ---------------------------------
// One card per object in the bucket, each with the SAME `⋮` the Files folder's
// cards use, and no visible destructive control anywhere. Three triggers rather
// than two is the assertion that matters: `from-another-machine` has no local
// artifact behind it, and gating on local state is what made a copy pushed
// elsewhere unremovable, so 3 is the fix, photographed.
await expectCount('library-card', 3)
await expectCount('library-more', 3)
// The item is inside a portaled menu, so it must NOT be in the document yet --
// which is also the point of the change: the destructive path is not on display.
await expectCount('library-remove', 0)
await expectCount('library-remove-confirm', 0)
// The never-pushed local artifact has nothing in the bucket, so this folder must
// not show it. A 4th card would mean the listing had drifted back to local state.
const bodyText = await page.locator('[data-testid="library-section"]').innerText().catch(() => '')
if (bodyText.includes('Draft onboarding')) failures.push('folder shows a never-pushed artifact')
await page.screenshot({ path: `${OUT}/library-remove-at-rest.png`, fullPage: false })
console.log('shot library-remove-at-rest')

// ---- frame (b): the menu open --------------------------------------------
// A hidden control that cannot be seen in a screenshot is the whole point of the
// change, so the menu gets a frame of its own.
if (await click('library-more')) await page.waitForTimeout(500)
await expectCount('library-remove', 1)
await expectCount('library-remove-confirm', 0)
await page.screenshot({ path: `${OUT}/library-remove-menu.png`, fullPage: false })
console.log('shot library-remove-menu')

// ---- frame (c): the confirm strip open ------------------------------------
if (await click('library-remove')) await page.waitForTimeout(500)
// Selecting the item closes the menu and opens the strip on THAT card. One
// confirm at a time is a property of holding the state on the SECTION rather
// than the card, so a second strip appearing here would mean that moved back.
await expectCount('library-remove-confirm', 1)
await expectCount('library-remove-confirm-cancel', 1)
await expectCount('library-remove-confirm-action', 1)
await expectCount('library-more', 3)
// The confirm must name BOTH the item and the prefix it empties: a generic "are
// you sure?" screenshots just as plausibly, and the prefix is the half that says
// which cloud folder actually goes. `artifacts/`, never `library/`.
const strip = await page.locator('[data-testid="library-remove-confirm"]').innerText().catch(() => '')
for (const [what, ok] of [
  ['confirm-names-item', /Cost dashboard|Release notes|from-another-machine/.test(strip)],
  ['confirm-names-artifacts-prefix', /artifacts\/[a-z-]+\//.test(strip)],
  ['confirm-avoids-library-prefix', !strip.includes('library/')],
]) {
  console.log(`ASSERT ${what} ${ok ? 'ok' : 'MISMATCH: ' + JSON.stringify(strip.slice(0, 200))}`)
  if (!ok) failures.push(`${what} failed on: ${strip.slice(0, 200)}`)
}
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
