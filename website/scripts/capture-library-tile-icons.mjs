/**
 * Screenshot harness for the Library launchpad's ICON GEOMETRY.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures via Playwright route interception —
 * gateway-free, no kiro-cli, no token.
 *
 * What it has to prove is an ASYMMETRY, so one app is not enough: an
 * app-supplied RASTER icon file must bleed to the tile's edges (it already IS a
 * finished 512x512 tile, and inset it reads as a small sticker on a dark plate),
 * while a first-party ``/app-assets/`` GLYPH and the lucide fallback must stay
 * inset at 30px, because line art needs that air and bleeding it would run its
 * strokes into the tile border. The frame therefore carries all three kinds side
 * by side — a change that bleeds everything looks wrong in the same picture.
 *
 * The raster fixture is served from an installed app's own local art route
 * (``/apps/<name>/art/<path>``), which is the URL shape ``installedIcon``
 * produces for a manifest ``iconPath`` — the case the report came from.
 *
 * ``deviceScaleFactor`` is 1, not 2: an image over 2000px on either edge is
 * rejected by the model provider, which wedges the conversation carrying it.
 *
 * Frames:
 *   library-tiles-dark.png   dark chrome  — the reported surface
 *   library-tiles-light.png  light chrome — the plate is light here, so a
 *                                           bled opaque tile must still keep a
 *                                           readable edge against the card
 *
 * Usage: node scripts/capture-library-tile-icons.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/library-tile-icons'
mkdirSync(OUT, { recursive: true })

/** A full-bleed opaque 512x512 tile — the shape the publishing guide specifies. */
const RASTER_TILE =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
  + '<rect width="512" height="512" fill="#16244a"/>'
  + '<circle cx="256" cy="248" r="150" fill="none" stroke="#d8b26a" stroke-width="10"/>'
  + '<path d="M256 330 L256 170 M256 230 L206 190 M256 230 L306 190" fill="none"'
  + ' stroke="#d8b26a" stroke-width="10" stroke-linecap="round"/>'
  + '<text x="256" y="452" text-anchor="middle" font-family="Helvetica,Arial"'
  + ' font-size="44" fill="#d8b26a">ICON FILE</text></svg>'

/** A first-party themeable glyph — line art, painted from theme tokens. */
const GLYPH_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
  + ' stroke="var(--ico-a)" stroke-width="1.6" stroke-linecap="round">'
  + '<rect x="3" y="4" width="18" height="7" rx="1.5"/>'
  + '<rect x="3" y="14" width="18" height="6" rx="1.5" stroke="var(--ico-b)"/>'
  + '<circle cx="7" cy="7.5" r="1" stroke="var(--ico-b)"/></svg>'

/** The local art route an installed manifest's `iconPath` resolves to. */
const RASTER_URL = '/apps/raster-tile-app/art/assets/icon.webp'
/** The first-party asset route a builtin's `iconUrl` declares. */
const GLYPH_URL = '/app-assets/glyph-svg-app/icon.svg'

const app = (name, displayName, description, manifest) => ({
  name,
  version: '1.0.0',
  enabled: true,
  installed: true,
  updateAvailable: false,
  origin: 'registry',
  resources: 'gateway',
  lifecycle: 'gateway',
  installedAt: '2026-08-01T00:00:00Z',
  manifest: {
    displayName,
    description,
    author: 'Kiro Crew', // brand-ok: fixture author
    ui: { pages: [{ route: `/apps/${name}`, label: displayName }] },
    ...manifest,
  },
})

const installed = [
  app('raster-tile-app', 'Icon file', 'Ships an icon FILE via iconPath — a finished opaque tile, so it bleeds to the edges.', {
    iconPath: 'assets/icon.webp',
  }),
  app('glyph-svg-app', 'Glyph SVG', 'Ships a first-party themeable /app-assets/ SVG — line art, so it stays inset.', {
    iconUrl: GLYPH_URL,
  }),
  app('lucide-glyph-app', 'Lucide glyph', 'Ships no icon file at all — a page icon name only, so it stays inset too.', {
    ui: { pages: [{ route: '/apps/lucide-glyph-app', label: 'Lucide glyph', icon: 'Shield' }] },
  }),
  app('no-icon-app', 'No icon', 'Ships neither — the tile degrades to a name-seeded gradient carrying a glyph.', {}),
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function shoot(mode, file) {
  const context = await browser.newContext({
    viewport: { width: 1180, height: 760 },
    deviceScaleFactor: 1,
    colorScheme: mode,
    // The built SPA registers /sw.js, and a request the service worker answers
    // never reaches `page.route` — the raster <img> happened to be issued before
    // the worker took control while the glyph's `fetch()` was not, so the glyph
    // silently received the SPA's index.html, failed the `startsWith('<svg')`
    // check, and left an empty icon box that read as a product bug.
    serviceWorkers: 'block',
  })
  const page = await context.newPage()
  logPageProblems(page)
  // The shared stub only intercepts `**/api/**`, and these two are ART routes
  // (an installed app's own local art path and a first-party /app-assets/
  // asset) — without their own handlers the static server answers them with the
  // SPA's index.html: the <img> then errors and degrades to the lucide glyph,
  // and the inline path's `startsWith('<svg')` check rejects the HTML and
  // leaves an EMPTY reserve box, so a missing handler quietly produces a frame
  // that looks like the bug. Matched by pathname predicate rather than a glob —
  // a leading `**` glob does not reliably match across the scheme and host.
  const art = { [RASTER_URL]: RASTER_TILE, [GLYPH_URL]: GLYPH_SVG }
  await page.route(
    (url) => Object.hasOwn(art, url.pathname),
    (route) => route.fulfill({
      status: 200,
      contentType: 'image/svg+xml',
      body: art[new URL(route.request().url()).pathname],
    }),
  )
  await stubDashboardApi(page, {
    theme: mode,
    extra: async (path, route) => {
      if (path === '/api/apps') return json(route, installed), true
      if (path === '/api/apps/registry') {
        return json(route, { apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } }), true
      }
      if (path === '/api/apps/registries') return json(route, { registries: [] }), true
      return false
    },
  })
  await page.addInitScript((m) => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme-mode', m)
  }, mode)

  await page.goto(`${base}/apps/library`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="launchpad-tile-raster-tile-app"]', { timeout: 15000 })
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/${file}` })

  // Evidence beyond the image: the icon's own box measured against the tile
  // face it sits in. A bled raster reads 58x58 (the tile), an inset glyph 30x30
  // — so the numbers, not just the pixels, carry the asymmetry.
  const geom = await page.evaluate(() => {
    const read = (name) => {
      const tile = document.querySelector(`[data-testid="launchpad-tile-${name}"]`)
      if (!tile) return null
      // The plate is the AppIconTile box — the only 58px square in the tile.
      // Selecting it by geometry rather than by class keeps the probe honest if
      // the classes move, and keeps it off the 18px pin badge, which is also a
      // rounded box carrying an <svg>.
      const plate = [...tile.querySelectorAll('div')]
        .find(d => Math.round(d.getBoundingClientRect().width) === 58)
      if (!plate) return { plate: null, art: null, kind: null }
      const art = plate.querySelector('img, span, svg')
      const p = plate.getBoundingClientRect()
      const a = art ? art.getBoundingClientRect() : null
      return {
        plate: [Math.round(p.width), Math.round(p.height)],
        art: a ? [Math.round(a.width), Math.round(a.height)] : null,
        kind: art ? art.tagName.toLowerCase() : null,
        // The plate carries a 1px border, so a bled image measures the tile's
        // width minus 2 — the border stays visible as the tile's own edge,
        // which is what keeps a light opaque icon from dissolving into light
        // chrome. Anything at or above that counts as filling.
        fills: !!(a && Math.round(a.width) >= Math.round(p.width) - 2),
      }
    }
    return {
      raster: read('raster-tile-app'),
      glyph: read('glyph-svg-app'),
      lucide: read('lucide-glyph-app'),
      none: read('no-icon-app'),
    }
  })
  console.log(`${file}  (${mode})`)
  console.log(JSON.stringify(geom))
  await context.close()
}

await shoot('dark', 'library-tiles-dark.png')
await shoot('light', 'library-tiles-light.png')

await browser.close()
srv.close()
console.log('done')
