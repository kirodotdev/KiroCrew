/**
 * Screenshot harness for a NOT-INSTALLED official-catalog app's detail-page art
 * (#6829).
 *
 * Runs the REAL built SPA behind `serveDist`, answers /api/** from fixtures via
 * `stubDashboardApi`. No gateway, no auth, no kiro-cli.
 *
 * The fixture is the shape #6829 is about: `atlas-notes` is an official-catalog
 * app the viewer has NOT installed. Its detail page resolves from the catalog
 * ROW alone (`/api/apps/<name>` 404s, `/api/apps/registry` supplies the row).
 * The catalog hosts its art on the CDN, so the row's `heroImageDetail` and
 * `screenshots` are absolute `https://apps.crew.kiro.dev/...` URLs the page
 * loads directly — never `/api/apps/blob`, which 403s for a repo the SSRF
 * allowlist does not carry.
 *
 * Two modes, because one frame cannot show a fix:
 *   --mode=after  (default) the catalog row CARRIES the CDN art fields (what the
 *                 fixed row builders emit). Asserts the detail banner and the
 *                 screenshot tile decode from their CDN URLs.
 *   --mode=before the row OMITS them (what the row builders emitted before the
 *                 fix, which mapped only iconRef/heroRef). Asserts neither the
 *                 detail banner nor a screenshot tile is on the page — the gap
 *                 the issue reports, from an unchanged bundle.
 *
 * `--dist=<path>` points at another bundle; the before frame does not need it
 * because the delta here is the ROW's fields, which this harness controls
 * directly, not the client code.
 *
 * Usage: node scripts/capture-catalog-detail-art.mjs [outDir] [--mode=before|after]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const flag = (name, fallback) => {
  const hit = args.find(a => a.startsWith(`--${name}=`))
  return hit ? hit.slice(name.length + 3) : fallback
}
const OUT = args.find(a => !a.startsWith('--')) || '/tmp/catalog-detail-art-shots'
const MODE = flag('mode', 'after')
const DIST = flag('dist', DEFAULT_DIST)
if (MODE !== 'before' && MODE !== 'after') {
  throw new Error(`--mode must be "before" or "after", got ${MODE}`)
}
mkdirSync(OUT, { recursive: true })

const APP = 'atlas-notes'
const CDN = 'https://apps.crew.kiro.dev'
const ICON = `${CDN}/assets/icons/atlas.png`
const HERO = `${CDN}/assets/heroes/atlas-hero.png`
const BANNER = `${CDN}/assets/hero-details/atlas-detail.png`
const SHOT = `${CDN}/assets/screenshots/atlas-1.png`

/** A flat gradient tile, sized per surface, so a decoded image is visibly ours. */
const art = (w, h, from, to, label) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}">`
  + `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">`
  + `<stop offset="0" stop-color="${from}"/><stop offset="1" stop-color="${to}"/>`
  + `</linearGradient></defs><rect width="${w}" height="${h}" fill="url(#g)"/>`
  + `<text x="${Math.round(w * 0.04)}" y="${Math.round(h * 0.62)}" font-family="Helvetica,Arial"`
  + ` font-size="${Math.round(h * 0.22)}" font-weight="700" fill="#fff" opacity=".92">${label}</text></svg>`

const CDN_ART = {
  [ICON]: art(512, 512, '#0b3d2e', '#10b981', 'AN'),
  [HERO]: art(1200, 675, '#0b3d2e', '#10b981', 'Atlas Notes'),
  [BANNER]: art(1200, 288, '#0b3d2e', '#10b981', 'Atlas Notes — banner'),
  [SHOT]: art(1200, 675, '#0b2f3d', '#22d3ee', 'Atlas Notes'),
}

/** The catalog row for a NOT-installed official-catalog app. Identity + display
 *  copy + CDN-hosted art, and NO clone coordinates (the catalog is TLS-trusted
 *  only). `iconUrl`/`heroImage` are what already worked before this change; the
 *  two fields under test (`heroImageDetail`, `screenshots`) are added only in
 *  `after`, so `before` shows the browse hero in the 16:9 container and no
 *  screenshots, and `after` shows the wide detail banner and the screenshot. */
function catalogRow() {
  const row = {
    name: APP,
    displayName: 'Atlas Notes',
    author: 'Atlas Labs',
    description: 'A not-installed official-catalog app whose art is CDN-hosted.',
    tags: ['notes'],
    version: '1.2.0',
    installed: false,
    iconUrl: ICON,
    heroImage: HERO,
    _catalog: true,
    source: { type: 'git' },
  }
  if (MODE === 'after') {
    row.heroImageDetail = BANNER
    row.screenshots = [SHOT]
  }
  return row
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1400, height: 1500 }, deviceScaleFactor: 2, serviceWorkers: 'block',
})
const page = await context.newPage()

/** Serve the cross-origin CDN art directly (stubDashboardApi handles only
 *  /api/**), so a rendered <img> decodes and the frame shows real art. */
await page.route(`${CDN}/**`, async route => {
  const body = CDN_ART[route.request().url()]
  await route.fulfill(body
    ? { status: 200, contentType: 'image/svg+xml', body }
    : { status: 404, body: '' })
})

const extra = async (path, route) => {
  if (path === '/api/apps/registry') {
    await json(route, { apps: [catalogRow()], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    return true
  }
  if (path === `/api/apps/${APP}`) {
    // NOT installed: the detail page resolves from the catalog row alone.
    await json(route, { error: 'not found' }, 404)
    return true
  }
  if (path === '/api/apps') {
    await json(route, [])
    return true
  }
  return false
}

await stubDashboardApi(page, { extra })
page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))

await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
await page.getByText('Atlas Notes').first().waitFor({ timeout: 15000 })
await page.waitForTimeout(2000)

function fail(msg) { throw new Error(`ASSERTION FAILED (${MODE}): ${msg}`) }

async function assertDecoded(label, src) {
  const img = page.locator(`img[src="${src}"]`).first()
  if (await img.count() === 0) fail(`${label}: no <img> with src ${src}`)
  const ok = await img.evaluate(el => el.complete && el.naturalWidth > 0)
  if (!ok) fail(`${label}: <img> ${src} did not decode`)
  console.log(`OK ${label}: ${src}`)
}

async function assertAbsent(label, src) {
  const n = await page.locator(`img[src="${src}"]`).count()
  if (n !== 0) fail(`${label}: expected no <img> for ${src}, found ${n}`)
  console.log(`OK ${label} absent: ${src}`)
}

if (MODE === 'after') {
  await assertDecoded('detail banner', BANNER)
  await assertDecoded('screenshot tile', SHOT)
} else {
  // The gap: the row carried neither field. The detail banner is absent (the
  // page falls back to the browse hero, shown here to prove the page rendered),
  // and there is no screenshot tile at all.
  await assertAbsent('detail banner', BANNER)
  await assertAbsent('screenshot tile', SHOT)
  await assertDecoded('browse hero (fallback)', HERO)
}

await page.screenshot({ path: `${OUT}/catalog-detail-${MODE}.png`, clip: { x: 0, y: 0, width: 1400, height: 1180 } })
console.log(`wrote ${OUT}/catalog-detail-${MODE}.png`)

await context.close()
await browser.close()
srv.close()
