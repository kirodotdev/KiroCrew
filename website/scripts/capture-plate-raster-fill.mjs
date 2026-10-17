/**
 * Screenshot harness for the two PLATE-DRAWING surfaces that inset an app's own
 * icon file: the detail page's 96px hero plate and the store spotlight's 92px
 * no-hero-art plate.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures via Playwright route interception —
 * gateway-free, no kiro-cli, no token.
 *
 * Both surfaces are captured because the change is one flag applied twice and
 * each plate needed a DIFFERENT prerequisite: the detail page already clipped and
 * only lacked `relative`; the spotlight plate lacked both `relative` and
 * `overflow-hidden`, and its own wrapper is the `relative aspect-[16/9]` art
 * panel — so a missing `relative` there does not merely misalign the icon, it
 * makes the icon fill the hero panel. The geometry probe prints the icon's box
 * against its plate, so that failure mode is caught by a number and not only by
 * a reviewer's eye.
 *
 * The context BLOCKS service workers: the built SPA registers /sw.js, and a
 * request the worker answers never reaches `page.route`, so an art fixture would
 * silently receive the SPA's index.html instead of the image.
 *
 * `deviceScaleFactor` is 1 — an image over 2000px on either edge is rejected by
 * the model provider, which wedges the conversation carrying it.
 *
 * Frames:
 *   detail-<mode>.png     the installed app's detail-page hero
 *   spotlight-<mode>.png  the Discover spotlight with an art-less lead app
 *
 * Usage: node scripts/capture-plate-raster-fill.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/plate-raster-fill'
mkdirSync(OUT, { recursive: true })

const APP = 'demo-app'
/** The local art route an installed manifest's `iconPath` resolves to. */
const RASTER_URL = `/apps/${APP}/art/assets/icon.webp`

/** A full-bleed opaque 512x512 tile — the shape the publishing guide specifies. */
const RASTER_TILE =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
  + '<rect width="512" height="512" fill="#16244a"/>'
  + '<circle cx="256" cy="248" r="150" fill="none" stroke="#d8b26a" stroke-width="10"/>'
  + '<path d="M256 330 L256 170 M256 230 L206 190 M256 230 L306 190" fill="none"'
  + ' stroke="#d8b26a" stroke-width="10" stroke-linecap="round"/>'
  + '<text x="256" y="452" text-anchor="middle" font-family="Helvetica,Arial"'
  + ' font-size="44" fill="#d8b26a">ICON FILE</text></svg>'

/** Installed, so the detail page resolves the icon from its own install dir. */
const installed = {
  name: APP,
  version: '1.0.0',
  enabled: true,
  installed: true,
  updateAvailable: false,
  origin: 'registry',
  resources: 'app',
  lifecycle: 'app',
  installedAt: '2026-08-01T00:00:00Z',
  manifest: {
    displayName: 'Demo App',
    description: 'Ships a 512x512 opaque icon file and no hero art.',
    author: 'Kiro Crew', // brand-ok: fixture author
    iconPath: 'assets/icon.webp',
    ui: { pages: [{ route: `/apps/${APP}`, label: 'Demo App' }] },
  },
}

/** The spotlight's lead row: an icon file and deliberately NO hero art, which is
 *  the branch that renders the 92px plate rather than an image. */
const registryRow = {
  name: APP,
  displayName: 'Demo App',
  description: 'Ships a 512x512 opaque icon file and no hero art, so the spotlight draws its plate.',
  author: 'Kiro Crew', // brand-ok: fixture author
  version: '1.0.0',
  tags: ['icons'],
  installed: false,
  updateAvailable: false,
  provenance: 'official',
  verified: true,
  iconUrl: RASTER_URL,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function shoot(mode, route, file, selector) {
  const context = await browser.newContext({
    viewport: { width: 1180, height: 820 },
    deviceScaleFactor: 1,
    colorScheme: mode,
    serviceWorkers: 'block',
  })
  const page = await context.newPage()
  logPageProblems(page)
  await page.route(
    (url) => url.pathname === RASTER_URL,
    (r) => r.fulfill({ status: 200, contentType: 'image/svg+xml', body: RASTER_TILE }),
  )
  await stubDashboardApi(page, {
    theme: mode,
    extra: async (path, r) => {
      if (path === '/api/apps') return json(r, [installed]), true
      if (path === `/api/apps/${APP}`) return json(r, installed), true
      if (path === '/api/apps/registry') {
        return json(r, { apps: [registryRow], serverPlatform: { os: 'linux', arch: 'x86_64' } }), true
      }
      if (path === '/api/apps/registries') return json(r, { registries: [] }), true
      return false
    },
  })
  await page.addInitScript((m) => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme-mode', m)
  }, mode)

  await page.goto(`${base}${route}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(selector, { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(1400)
  await page.screenshot({ path: `${OUT}/${file}` })

  // Evidence beyond the image: the icon's own box against the plate it sits in,
  // plus the plate's box against the surface, so an icon that escaped its plate
  // (the missing-`relative` failure) shows up as a number, not just a look.
  //
  // Selecting by `src` alone is NOT enough: the sidebar rail renders the same
  // app's icon at 16px, and it comes first in the document — an earlier version
  // of this probe measured that and reported a 16px "plate" on both surfaces.
  // The target is the LARGEST occurrence, and every occurrence is printed so a
  // future reader can see what else on the page shares the bytes.
  const geom = await page.evaluate((src) => {
    const r = (el) => { const b = el.getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)] }
    const all = [...document.querySelectorAll('img')].filter(i => i.getAttribute('src') === src)
    if (!all.length) return { img: null, occurrences: 0 }
    const img = all.reduce((a, b) => (r(b)[0] > r(a)[0] ? b : a))
    const plate = img.parentElement
    return {
      occurrences: all.map(r),
      img: r(img),
      plate: plate ? r(plate) : null,
      plateClasses: plate ? plate.className : null,
      escapedPlate: plate ? r(img)[0] > r(plate)[0] : null,
    }
  }, RASTER_URL)
  console.log(`${file}  (${mode})  ${JSON.stringify(geom)}`)
  await context.close()
}

for (const mode of ['dark', 'light']) {
  await shoot(mode, `/apps/detail/${APP}`, `detail-${mode}.png`, 'img, .w-24')
  await shoot(mode, '/apps', `spotlight-${mode}.png`, 'img')
}

await browser.close()
srv.close()
console.log('done')
