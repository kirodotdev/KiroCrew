/**
 * Screenshot harness for the theme-aware app icon and the `official` provenance
 * label.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures via Playwright route interception —
 * gateway-free, no kiro-cli, no token.
 *
 * What it has to prove, and why one frame cannot: `AppIcon` picks `iconUrlDark`
 * under a dark theme and `iconUrl` under a light one, falling back in either
 * direction. That is a change of BYTES, not of CSS, so the only honest evidence
 * is the same rows captured twice with the theme flipped.
 *
 * The fixtures cover all four icon cases in one frame:
 *   both variants   light + dark    -> the variant is used
 *   light only      no dark         -> reuses the light bytes on dark chrome
 *   dark only       no light        -> falls back the OTHER way, so a light
 *                                      theme still gets an icon
 *   neither         no icon key     -> name-seeded gradient + glyph
 *
 * `deviceScaleFactor` is 1, not 2: at this viewport a 2x capture is 2800px wide,
 * and an image over 2000px on either edge is rejected by the model provider,
 * which wedges the conversation carrying it.
 *
 * Frames:
 *   icons-light.png  light chrome — hero tile and rows resolve the light bytes
 *   icons-dark.png   dark chrome  — the same rows resolve the dark bytes
 *
 * Usage: node scripts/capture-app-icon-theme.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/app-icon-theme'
mkdirSync(OUT, { recursive: true })

/** An opaque 512x512 tile — the shape the publishing guide now specifies. */
const tile = (bg, fg, glyph) =>
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
  + `<rect width="512" height="512" rx="96" fill="${bg}"/>`
  + '<text x="256" y="336" text-anchor="middle" font-family="Helvetica,Arial"'
  + ` font-size="240" font-weight="700" fill="${fg}">${glyph}</text></svg>`

/** blob-proxy repo key -> the bytes the stub returns for it. */
const ICONS = {
  'both-light': tile('#f1f5f9', '#0f172a', 'A'),
  'both-dark': tile('#0f172a', '#f1f5f9', 'A'),
  'light-only': tile('#fde68a', '#7c2d12', 'B'),
  'dark-only': tile('#1e1b4b', '#a5b4fc', 'C'),
}
const blob = (key) => `/api/apps/blob?repo=${key}&path=icon.svg`

const A = (name, displayName, description, extra = {}) => ({
  name, displayName, author: 'Kiro Crew', description, tags: ['icons'],
  version: '1.0.0', installed: false, updateAvailable: false,
  provenance: 'official', verified: true, ...extra,
})

const registryApps = [
  A('both-icons', 'Both variants', 'Ships iconPath AND iconPathDark — the dark tile replaces the light one when the theme flips.', {
    iconUrl: blob('both-light'), iconUrlDark: blob('both-dark'),
  }),
  A('light-only', 'Light variant only', 'Ships iconPath only — the same opaque bytes are reused on dark chrome, which is why the dark variant is optional.', {
    iconUrl: blob('light-only'),
  }),
  A('dark-only', 'Dark variant only', 'Ships iconPathDark only — resolution falls back in BOTH directions, so a light theme still gets an icon.', {
    iconUrlDark: blob('dark-only'),
  }),
  A('no-icon', 'No icon at all', 'Ships neither key — the row degrades to a name-seeded gradient carrying a glyph, never a blank tile.', {}),
  A('external-app', 'A third-party app', 'Carries provenance external, so it gets no verified mark no matter what its manifest claims.', {
    provenance: 'external', verified: false, _registry: 'labs',
  }),
]

const { srv, base } = await serveDist()

const browser = await chromium.launch()

async function shoot(mode, file) {
  const context = await browser.newContext({
    viewport: { width: 1400, height: 940 },
    deviceScaleFactor: 1,
    colorScheme: mode,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: mode,
    extra: async (path, route) => {
      if (path === '/api/apps/blob') {
        const key = new URL(route.request().url()).searchParams.get('repo') || ''
        const body = ICONS[key]
        if (!body) return route.fulfill({ status: 404, body: '' }), true
        return route.fulfill({ status: 200, contentType: 'image/svg+xml', body }), true
      }
      if (path === '/api/apps/registry') {
        return json(route, { apps: registryApps, serverPlatform: { os: 'darwin', arch: 'arm64' } }), true
      }
      if (path === '/api/apps/registries') return json(route, { registries: [] }), true
      return false
    },
  })
  await page.addInitScript((m) => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme-mode', m)
  }, mode)

  await page.goto(`${base}/apps`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await page.screenshot({ path: `${OUT}/${file}` })
  console.log(`${file}  (${mode})`)
  await context.close()
}

await shoot('light', 'icons-light.png')
await shoot('dark', 'icons-dark.png')

await browser.close()
srv.close()
console.log('done')
