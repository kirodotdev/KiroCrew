/**
 * Screenshot + measurement harness for the AWS Control remote crews pane.
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call answered from fixtures - no gateway, no
 * dashboard token. Same technique as capture-aws-control.mjs, which this is
 * modelled on; it answers the same dashboard boot endpoints for the same reason
 * (the shell mounts before the app page, and a list endpoint answered with an
 * object crashes its error boundary so the app page never mounts at all).
 *
 * The reason it exists rather than living in vitest: jsdom reports every layout
 * box as zero, so no unit test can prove that two cards in a row line up. Here a
 * real engine lays them out and the numbers are read back.
 *
 * Captures:
 *   crews.png            the grid, with a badge-carrying card beside a bare one
 *   crews-detail.png     one crew opened, with running/desired and the endpoint
 *   crews-base.png       the no-base-stack state (not "no crews")
 *   crews-empty.png      the base is ready and the account holds none
 *   crews-mismatch.png   account_mismatch, with its own copy
 *   crews-narrow.png     the pane at 320px, pushed level with one back bar
 *
 * deviceScaleFactor is 1.5, not 2: a multi-image read refuses anything over
 * 2000px on a side, and 1280 x 2 is 2560.
 *
 * Usage: node scripts/capture-aws-control-crews.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/aws-control-crews'
mkdirSync(OUT, { recursive: true })

// ---- fixtures -------------------------------------------------------------
const ACCOUNTS = {
  supported: true,
  accounts: [
    {
      account: '111122223333', name: 'personal', health: 'ok',
      profiles: [{ name: 'personal', kind: 'credential-process', region: 'us-west-2', account: '111122223333', default: true, identityOk: true }],
    },
  ],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
}

const DRIVE = {
  exists: true, bucket: 'kirocrew-drive-7f3a91c4', region: 'us-west-2',
  usage: {
    bytes: 44677427, objects: 18,
    sections: {
      drive: { objects: 4, bytes: 32715570 },
      library: { objects: 6, bytes: 11157402 },
      backup: { objects: 8, bytes: 804455 },
    },
  },
}

/**
 * One crew as the LIST route answers: service empty, counts zero, so `healthy`
 * is false for every one of them. The fixture keeps that honest on purpose - a
 * fixture that filled the counts in would hide the very thing the card must not
 * render.
 *
 * The stack name is DERIVED from the crew name, the way `crews.py` parses it back
 * out of `smc-crew-<name>`. An earlier version hardcoded one stack for all four,
 * which made the Stack cell look like a constant and hid whether it was readable.
 *
 * The image is digest-pinned with a full 64 hex characters, because the template's
 * `AllowedPattern` (`.+@sha256:[a-f0-9]{64}$`) refuses a tag. A short fake digest
 * would understate the length the card has to cope with.
 */
const DIGEST = '9f1c2d3e4b5a67788990aabbccddeeff00112233445566778899aabbccddeeff'
const listCrew = (name, over = {}) => ({
  name,
  stack: `smc-crew-${name}`,
  stackStatus: 'CREATE_COMPLETE',
  memory: 'chatbot',
  service: '',
  running: 0,
  desired: 0,
  healthy: false,
  image: `111122223333.dkr.ecr.us-west-2.amazonaws.com/smc@sha256:${DIGEST}`,
  controlBase: 'https://d1abcdefghij.cloudfront.net',
  region: 'us-west-2',
  ...over,
})

// Four crews chosen so the grid shows every case the pane has to get right in
// one frame: a settled one with no badge, one mid-update (warn), one mid-delete
// (err, listed on purpose), and one whose stack predates the Memory parameter.
//
// The ORDER is the backend's (sorted by name), and the names are picked so that
// alphabetical order puts a badge-carrying card beside a bare one in BOTH rows of
// the two-column grid. That is what makes the alignment measurement below test the
// mixed case instead of passing vacuously on a uniform row.
const CREWS = {
  account: '111122223333',
  region: 'us-west-2',
  baseMissing: false,
  crews: [
    listCrew('billing-help', { memory: 'persistent' }),
    listCrew('checkout-bot', { stackStatus: 'UPDATE_IN_PROGRESS' }),
    listCrew('legacy-triage', { memory: '', image: '', controlBase: '' }),
    listCrew('winter-promo', { stackStatus: 'DELETE_IN_PROGRESS', memory: 'persistent' }),
    // A near-maximum name (the backend's own pattern allows 32 characters), which
    // is what the clipping measurement below is really aimed at: this is the crew
    // whose `smc-crew-<name>` stack did not fit while the cell was half a card
    // wide, and the card that proves it fits now.
    listCrew('regional-returns-desk-au-south', { memory: 'persistent' }),
  ],
}

const DETAIL = listCrew('billing-help', {
  memory: 'persistent',
  service: 'smc-billing-help', running: 2, desired: 2, healthy: true,
})

// Flipped between phases so one page can photograph every state.
let mode = 'list'

const BASE = '/api/apps/aws-control'

/**
 * The dashboard shell's own boot endpoints, as a TABLE rather than an if-chain.
 *
 * The shell mounts before the app page and consumes several of these as ARRAYS,
 * so a blanket `{}` crashes its error boundary ("x.filter is not a function") and
 * the app page never mounts at all. Every harness in this directory therefore has
 * to answer the same set with the same shapes.
 *
 * A table because of that: written as the chain of `if (path === ...)` lines its
 * siblings use, this block IS a 406-token clone of
 * `capture-aws-control-library-remove.mjs`, which the duplication gate reports.
 * The same remedy `capture-prose-diff-fold.mjs` used - keep the payload, drop the
 * repeated control flow - removes the clone without an exemption entry.
 */
const BOOT = {
  '/api/apps': [],
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/status': { sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, uptime: 1, version: '0.1.0' },
  '/api/kiro-prerequisite': { installed: true, authenticated: true, ready: true },
  '/api/dashboard/branding': { bot_name: 'Kiro Crew', avatar: '' },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/themes': { themes: [], installed: [] },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/chat/slots': [],
  '/api/models': { models: [], default: 'auto' },
}

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

async function answer(route) {
  const path = new URL(route.request().url()).pathname
  if (path.endsWith('/accounts')) return json(route, ACCOUNTS)
  if (path === '/api/aws/consent') {
    return json(route, { service: 's3', granted: true, region: 'us-west-2', account: '111122223333' })
  }
  const app = path.startsWith(BASE) ? path.slice(BASE.length) : ''
  if (/^\/crews\/[^/]+\/[^/]+$/.test(app)) return json(route, DETAIL)
  if (/^\/crews\/[^/]+$/.test(app)) {
    if (mode === 'base') return json(route, { ...CREWS, baseMissing: true, crews: [] })
    if (mode === 'empty') return json(route, { ...CREWS, crews: [] })
    if (mode === 'mismatch') {
      return json(route, { error: 'profile resolves to account 000000000000', code: 'account_mismatch' }, 409)
    }
    return json(route, CREWS)
  }
  if (/^\/drive\/[^/]+$/.test(app)) return json(route, DRIVE)
  if (/^\/drive\/[^/]+\/list$/.test(app)) return json(route, { folders: [], files: [] })
  if (/^\/costs\/[^/]+$/.test(app)) return json(route, { monthToDate: 2.25, currency: 'USD', fresh: true, fetchedAt: new Date().toISOString(), byService: [] })
  if (app === '/profiles/available') return json(route, { supported: true, profiles: [], max: 20, registeredCount: 1 })
  if (/^\/library\/[^/]+$/.test(app)) return json(route, { artifacts: [] })
  if (/^\/backup\/[^/]+$/.test(app)) return json(route, { nightly: false, runs: {}, remote: null, jobs: {} })
  if (app.startsWith('/shares')) return json(route, { shares: [] })
  if (path in BOOT) return json(route, BOOT[path])
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  // Unknown paths: object-ish names get {}, everything else an array, because a
  // list endpoint answered with an object is what crashes the shell.
  const objectish = /(config|tips|voice|autonudge|branding|status|themes|system)/.test(path)
  return json(route, objectish ? {} : [])
}

// ---- run ------------------------------------------------------------------
const { srv: server, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1.5 })
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
// every screenshot showed the old page.
const failures = []
const expectCount = async (t, want) => {
  const got = await page.locator(`[data-testid="${t}"]`).count()
  const ok = got === want
  console.log(`ASSERT ${t} want=${want} got=${got} ${ok ? 'ok' : 'MISMATCH'}`)
  if (!ok) failures.push(`${t}: want ${want}, got ${got}`)
}
const reload = async (m, pane = 'crews') => {
  mode = m
  await page.goto(`${base}/aws-control/${pane}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1200)
}

// ---- the grid -------------------------------------------------------------
await reload('list')
await expectCount('crews-pane', 1)
await expectCount('aws-rail', 1)
await expectCount('rail-crews', 1)
// No count beside the rail item: the inventory is read when the pane opens, not
// on every app load, so there is no number to show before then.
await expectCount('rail-crews-count', 0)
await expectCount('crews-grid', 1)
await expectCount('crew-card', 5)
await expectCount('crews-blurb', 1)
// The pane title is the LONG form. The rail may say the short word; a title
// travels, and the agents page calls its own local agents crews in code.
const title = ((await page.locator('[data-testid="page-title"]').first().textContent()) || '').trim()
console.log(`ASSERT pane title = ${title} ${/remote/i.test(title) ? 'ok' : 'MISMATCH'}`)
if (!/remote/i.test(title)) failures.push(`pane title does not say remote: ${title}`)

// The mid-delete crew is present and visibly not healthy, not filtered out.
const dying = page.locator('[data-testid="crew-card"][data-status="DELETE_IN_PROGRESS"]')
const dyingCount = await dying.count()
console.log(`ASSERT deleting crew listed want=1 got=${dyingCount} ${dyingCount === 1 ? 'ok' : 'MISMATCH'}`)
if (dyingCount !== 1) failures.push(`deleting crew: want 1 listed, got ${dyingCount}`)
const dyingTinted = dyingCount === 1 && (await dying.first().getAttribute('class') || '').includes('border-danger')
console.log(`ASSERT deleting crew tinted ${dyingTinted ? 'ok' : 'MISMATCH'}`)
if (!dyingTinted) failures.push('deleting crew is listed but does not read as unhealthy')

// ---- THE MEASUREMENT: does a row stay aligned? ----------------------------
// The grid is 2 columns at 1280px, and the fixture names are ordered so each row
// holds one badge-carrying card and one bare one. If the header sized itself to
// its content the badge card's fact grid would start lower than its neighbour's,
// which is exactly what is measured: the TOP of each card's fact grid, relative
// to the top of its own card.
const align = await page.evaluate(() => {
  const cards = [...document.querySelectorAll('[data-testid="crew-card"]')]
  const rows = new Map()
  for (const card of cards) {
    const cr = card.getBoundingClientRect()
    const header = card.querySelector('[data-testid="crew-card-header"]')
    const facts = card.querySelector('[data-testid="crew-card-facts"]')
    if (!header || !facts) return { error: 'a card is missing its header or fact grid' }
    const rowKey = Math.round(cr.top)
    if (!rows.has(rowKey)) rows.set(rowKey, [])
    rows.get(rowKey).push({
      name: card.getAttribute('data-crew'),
      hasBadge: Boolean([...header.querySelectorAll('span')].some(
        (s) => s.className.includes('rounded-full') && s.textContent.trim(),
      )),
      headerH: Math.round(header.getBoundingClientRect().height),
      // Offset of the fact grid inside its own card. THIS is the number a
      // ragged row shows up in.
      factsOffset: Math.round(facts.getBoundingClientRect().top - cr.top),
      cardH: Math.round(cr.height),
    })
  }
  return { rows: [...rows.entries()].map(([top, cards]) => ({ top, cards })) }
})

if (align.error) {
  failures.push(`alignment: ${align.error}`)
} else {
  for (const { top, cards } of align.rows) {
    const offsets = [...new Set(cards.map((c) => c.factsOffset))]
    const heights = [...new Set(cards.map((c) => c.headerH))]
    const badged = cards.filter((c) => c.hasBadge).length
    console.log(`ROW top=${top} cards=${cards.map((c) => `${c.name}${c.hasBadge ? '[badge]' : ''}@${c.factsOffset}`).join(' ')}`)
    if (offsets.length !== 1) {
      failures.push(`alignment: row at ${top} has fact grids at different offsets (${offsets.join(', ')})`)
    }
    if (heights.length !== 1) {
      failures.push(`alignment: row at ${top} has header blocks of different heights (${heights.join(', ')})`)
    }
    // A row where every card is badged (or none is) does not test anything, so
    // say so rather than reporting a vacuous pass.
    if (cards.length > 1 && (badged === 0 || badged === cards.length)) {
      console.log(`  (row at ${top} is uniform: ${badged}/${cards.length} badged - not the mixed case)`)
    } else if (cards.length > 1) {
      console.log(`  (row at ${top} IS the mixed case: ${badged}/${cards.length} badged, offsets agree at ${offsets[0]}px)`)
    }
  }
  const mixed = align.rows.some((r) => {
    const badged = r.cards.filter((c) => c.hasBadge).length
    return r.cards.length > 1 && badged > 0 && badged < r.cards.length
  })
  console.log(`ASSERT a mixed badge/no-badge row was actually rendered ${mixed ? 'ok' : 'MISMATCH'}`)
  if (!mixed) failures.push('alignment: no row mixed a badged card with a bare one, so nothing was proven')
}

await page.screenshot({ path: `${OUT}/crews.png`, fullPage: false })
console.log('shot crews')

// ---- THE OTHER MEASUREMENT: is anything on the card actually readable? ----
// The first run of this harness photographed four cards whose Stack cell read
// `smc-crew-s…` on every one of them and whose Endpoint and Image were pure
// ellipsis - four labels with no facts under them. A count assertion cannot see
// that, so the clipping is measured: a value is clipped when its own box scrolls.
//
// MODE is exempt: it is a translated phrase, and a locale whose word for "keeps
// memory" is long enough to clip is a copy problem for that locale, not a layout
// regression to fail an English capture on.
const clip = await page.evaluate(() => {
  const bad = []
  for (const card of document.querySelectorAll('[data-testid="crew-card"]')) {
    const name = card.getAttribute('data-crew')
    for (const t of ['crew-stack', 'crew-endpoint', 'crew-image']) {
      const el = card.querySelector(`[data-testid="${t}"]`)
      if (!el) { bad.push(`${name}/${t}: missing`); continue }
      if (el.scrollWidth > el.clientWidth + 1) {
        bad.push(`${name}/${t}: needs ${el.scrollWidth}px, has ${el.clientWidth}px -> "${el.textContent.trim()}"`)
      }
    }
  }
  return bad
})
console.log(`ASSERT no fact value is clipped ${clip.length === 0 ? 'ok' : 'MISMATCH'}`)
for (const c of clip) console.log(`  CLIPPED ${c}`)
if (clip.length) failures.push(`clipped fact values: ${clip.join('; ')}`)

// ---- one crew opened ------------------------------------------------------
await page.locator('[data-testid="crew-card"][data-crew="billing-help"]').click()
await page.waitForTimeout(800)
await expectCount('crew-detail', 1)
await expectCount('crew-detail-facts', 1)
// The detail is the ONLY surface that shows the serving counts, because it is
// the only one that paid the ECS call for them.
await expectCount('crew-detail-tasks', 1)
await expectCount('crew-copy-endpoint', 1)
await expectCount('crew-copy-image', 1)
await expectCount('crews-grid', 0)
await page.screenshot({ path: `${OUT}/crews-detail.png`, fullPage: false })
console.log('shot crews-detail')
// A breadcrumb, not a route: the URL never changed, so back is in-pane state.
const url = page.url()
console.log(`ASSERT detail is view state, not a route url=${url} ${url.endsWith('/aws-control/crews') ? 'ok' : 'MISMATCH'}`)
if (!url.endsWith('/aws-control/crews')) failures.push(`detail changed the URL to ${url}`)
await page.locator('[data-testid="crew-detail-back"]').click()
await page.waitForTimeout(500)
await expectCount('crews-grid', 1)
await expectCount('crew-detail', 0)

// ---- the three non-error states ------------------------------------------
await reload('base')
await expectCount('crews-base-missing', 1)
await expectCount('crews-empty', 0)
await expectCount('crews-error', 0)
await page.screenshot({ path: `${OUT}/crews-base.png`, fullPage: false })
console.log('shot crews-base')

await reload('empty')
await expectCount('crews-empty', 1)
await expectCount('crews-base-missing', 0)
await expectCount('crews-error', 0)
await page.screenshot({ path: `${OUT}/crews-empty.png`, fullPage: false })
console.log('shot crews-empty')

await reload('mismatch')
await expectCount('crews-mismatch', 1)
await expectCount('crews-error', 0)
await expectCount('crews-empty', 0)
await expectCount('crews-base-missing', 0)
await page.screenshot({ path: `${OUT}/crews-mismatch.png`, fullPage: false })
console.log('shot crews-mismatch')

// ---- narrow viewport -----------------------------------------------------
// The pane is a pushed level on a phone, with one back bar and no rail, and the
// grid collapses to one column so no card is clipped.
await reload('list')
await page.setViewportSize({ width: 320, height: 900 })
await page.waitForTimeout(500)
await expectCount('aws-pane-detail', 1)
await expectCount('aws-rail', 0)
await expectCount('crew-card', 5)
const clipped = await page.evaluate(() => {
  const bad = []
  for (const card of document.querySelectorAll('[data-testid="crew-card"]')) {
    const r = card.getBoundingClientRect()
    if (r.right > window.innerWidth + 1 || r.left < -1) {
      bad.push(`${card.getAttribute('data-crew')}: ${Math.round(r.left)}..${Math.round(r.right)} outside 0..${window.innerWidth}`)
    }
  }
  return bad
})
console.log(`ASSERT narrow cards on screen ${clipped.length === 0 ? 'ok' : 'MISMATCH ' + clipped.join('; ')}`)
if (clipped.length) failures.push(`narrow viewport: ${clipped.join('; ')}`)
await page.screenshot({ path: `${OUT}/crews-narrow.png`, fullPage: false })
console.log('shot crews-narrow')

await browser.close()
server.close()
if (failures.length) {
  console.error('harness assertions failed (stale dist, or the UI changed):')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('done')
