/**
 * Screenshot harness for an installed app's own manifest art on the detail page.
 *
 * Runs the REAL built SPA behind the shared `serveDist` server and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no dashboard
 * auth, no kiro-cli.
 *
 * The fixture is the shape that breaks: `pixel-pal` is installed from a registry,
 * its registry ROW carries no art fields (what a manifest cache older than the
 * release that added the art produces), and its installed MANIFEST declares
 * repo-relative `iconPath` / `heroImage*` / `screenshots`. Only the blob proxy can
 * serve those paths, so the page must resolve them; handing them to `<img>` as
 * written resolves against the current route and silently fails.
 *
 * Two modes, because one frame cannot show a fix:
 *   --mode=after  (default) asserts each art surface requests the blob proxy AND
 *                 that the image decoded
 *   --mode=before asserts the defect is on screen — no blob-proxy request at all —
 *                 so a "before" frame captured from an older bundle is verified to
 *                 show the bug rather than merely claimed to
 *
 * `--dist=<path>` points at a bundle other than `website/dist`, which is how the
 * before frame is captured from a bundle built at the base commit.
 *
 * Usage: node scripts/capture-app-detail-art.mjs [outDir] [--mode=before|after] [--dist=path]
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
const OUT = args.find(a => !a.startsWith('--')) || '/tmp/app-detail-art-shots'
const MODE = flag('mode', 'after')
const DIST = flag('dist', DEFAULT_DIST)
if (MODE !== 'before' && MODE !== 'after') {
  throw new Error(`--mode must be "before" or "after", got ${MODE}`)
}
mkdirSync(OUT, { recursive: true })

const APP = 'pixel-pal'
const REPO = 'https://example.invalid/octocat/pixel-pal'
const ICON = 'assets/icon.svg'
const BANNER = 'assets/hero-detail.svg'
const SHOT = 'assets/screenshots/one.svg'

const proxied = path =>
  `/api/apps/blob?repo=${encodeURIComponent(REPO)}&path=${encodeURIComponent(path)}`

/** The registry row an art-less manifest cache produces: identity only, no art. */
const registryRow = {
  name: APP, displayName: 'Pixel Pal', author: 'octocat',
  description: 'A registry app whose art is declared only in its own manifest.',
  tags: ['images'], version: '1.0.0', installed: true, enabled: true,
  origin: 'registry', lifecycle: 'gateway', repo: REPO, _registry: 'sample-registry',
}

const installedApp = {
  name: APP, displayName: 'Pixel Pal', version: '1.0.0', enabled: true,
  installedAt: '2026-07-20T10:00:00Z', origin: 'registry',
  resources: 'gateway', lifecycle: 'gateway', sourceUrl: REPO,
  manifest: {
    name: APP, version: '1.0.0', displayName: 'Pixel Pal',
    description: 'A registry app whose art is declared only in its own manifest.',
    author: 'octocat', tags: ['images'],
    highlights: ['Ships an icon, a wide detail banner, and a screenshot'],
    iconPath: ICON, heroImage: 'assets/hero.svg', heroImageDetail: BANNER,
    screenshots: [SHOT],
    ui: { pages: [{ route: `/${APP}`, label: 'Pixel Pal', iconUrl: 'icon.svg' }] },
  },
}

/** A flat SVG tile, sized per surface, so a decoded image is visibly the fixture. */
const art = (w, h, from, to, label) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}">`
  + `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">`
  + `<stop offset="0" stop-color="${from}"/><stop offset="1" stop-color="${to}"/>`
  + `</linearGradient></defs><rect width="${w}" height="${h}" fill="url(#g)"/>`
  + `<text x="${Math.round(w * 0.04)}" y="${Math.round(h * 0.62)}" font-family="Helvetica,Arial"`
  + ` font-size="${Math.round(h * 0.22)}" font-weight="700" fill="#fff" opacity=".92">${label}</text></svg>`

const BLOB_ART = {
  [ICON]: art(512, 512, '#2e1f57', '#6d4aff', 'PP'),
  [BANNER]: art(1200, 288, '#2e1f57', '#6d4aff', 'Pixel Pal'),
  [SHOT]: art(1200, 675, '#123b46', '#22d3ee', 'Pixel Pal'),
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
// The built SPA registers a service worker whose fetch handler would bypass
// page.route for non-/api requests — block it.
const context = await browser.newContext({
  viewport: { width: 1400, height: 1500 }, deviceScaleFactor: 2, serviceWorkers: 'block',
})
const page = await context.newPage()

/** Every blob-proxy request the page made, so the assertion names the wire. */
const blobHits = []

/** Each branch AWAITS `json()` (or fulfills) then returns true; falsy = not handled. */
const extra = async (path, route) => {
  if (path === '/api/apps/blob') {
    const params = new URL(route.request().url()).searchParams
    const repo = params.get('repo')
    const blobPath = params.get('path') || ''
    blobHits.push({ repo, path: blobPath })
    const body = repo === REPO ? BLOB_ART[blobPath] : undefined
    await route.fulfill(body
      ? { status: 200, contentType: 'image/svg+xml', body }
      : { status: 404, body: '' })
    return true
  }
  if (path === '/api/apps/registry') {
    await json(route, { apps: [registryRow], serverPlatform: { os: 'linux', arch: 'x86_64' } })
    return true
  }
  if (path === `/api/apps/${APP}`) {
    await json(route, installedApp)
    return true
  }
  if (path === '/api/apps') {
    await json(route, [installedApp])
    return true
  }
  return false
}

await stubDashboardApi(page, { extra })
page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))

await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
await page.getByText('Pixel Pal').first().waitFor({ timeout: 15000 })
await page.waitForTimeout(2000)

function fail(msg) { throw new Error(`ASSERTION FAILED (${MODE}): ${msg}`) }

/** Assert an <img> with the exact src exists and actually decoded. */
async function assertDecoded(label, src) {
  const img = page.locator(`img[src="${src}"]`).first()
  if (await img.count() === 0) fail(`${label}: no <img> with src ${src}`)
  const ok = await img.evaluate(el => el.complete && el.naturalWidth > 0)
  if (!ok) fail(`${label}: <img> ${src} did not decode`)
  console.log(`OK ${label}: ${src}`)
}

if (MODE === 'after') {
  await assertDecoded('detail banner', proxied(BANNER))
  await assertDecoded('screenshot tile', proxied(SHOT))
  await assertDecoded('app icon', proxied(ICON))
  for (const path of [BANNER, SHOT, ICON]) {
    if (!blobHits.some(h => h.repo === REPO && h.path === path)) {
      fail(`blob proxy never queried for ${path} — hits: ${JSON.stringify(blobHits)}`)
    }
  }
} else {
  if (blobHits.length > 0) {
    fail(`expected the defect (no proxy request), but the page queried: ${JSON.stringify(blobHits)}`)
  }
  // The manifest paths reach the DOM raw, which is what makes them unfetchable.
  const raw = await page.evaluate(() => [...document.querySelectorAll('img')]
    .map(i => i.getAttribute('src') || '')
    .filter(s => s.startsWith('assets/')))
  console.log(`OK defect present: no blob-proxy request; raw srcs on page: ${JSON.stringify(raw)}`)
}

// Clipped to the surfaces under test — banner, icon, screenshots strip — so the
// two frames differ only where the fix lands instead of in shared empty page.
await page.screenshot({ path: `${OUT}/detail-${MODE}.png`, clip: { x: 0, y: 0, width: 1400, height: 1180 } })
console.log(`wrote ${OUT}/detail-${MODE}.png`)

await context.close()
await browser.close()
srv.close()
