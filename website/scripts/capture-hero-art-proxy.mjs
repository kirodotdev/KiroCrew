/**
 * Asserting screenshot harness for repo-relative hero-art resolution (#1255).
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, every /api/** call intercepted by Playwright and answered from
 * fixtures — no gateway, no dashboard token. Same technique as
 * capture-apps.mjs.
 *
 * Fixture: "pixel-pal" is a registry-installed app whose installed manifest
 * declares RELATIVE hero paths (assets/hero-dark.svg) plus a repo — the #1255
 * bug case. The registry row for it carries no hero art, so Discover's
 * client-side merge pulls the relative manifest paths too. "research-lab" is a
 * builtin with an ABSOLUTE /app-assets/... hero proving pass-through.
 *
 * The script ASSERTS (throws on failure) before each capture:
 *   - the relative hero renders with a blob-proxy src on Library AND Discover
 *   - the blob proxy was queried with the exact repo + path pair
 *   - the absolute builtin hero src is untouched
 *
 * Captures: discover-dark.png, library-dark.png, library-light.png
 *
 * Usage: node scripts/capture-hero-art-proxy.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/hero-art-shots'
const PORT = 6813
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' }
const isFile = (p) => { try { return statSync(p).isFile() } catch { return false } }
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  if (path === '/logo.png') {
    res.writeHead(200, { 'content-type': 'image/png' })
    res.end(readFileSync(join(DIST, 'icon-192.png')))
    return
  }
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) {
    res.writeHead(403); res.end(); return
  }
  if (path === '/' || !isFile(file)) file = join(DIST, 'index.html')
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch {
    res.writeHead(404); res.end()
  }
})
await new Promise(r => server.listen(PORT, '127.0.0.1', r))

const status = { sessions: 12, messages: 4821, cron_jobs: 7, subagents: 3, lessons: 52, uptime: 273840, version: '0.1.0' }
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const REPO = 'octocat/pixel-pal'
const REG = 'kirodotdev-labs'

// Registry rows: pixel-pal carries NO hero fields (the merge in AppsPage pulls
// them from the installed manifest — the relative-path case under test).
const registryApps = [
  {
    name: 'pixel-pal', displayName: 'Pixel Pal', author: 'octocat',
    description: 'A registry app whose manifest declares repo-relative hero art.',
    tags: ['images'], version: '1.0.0', installed: true, enabled: true,
    origin: 'registry', lifecycle: 'gateway', repo: REPO, _registry: REG, featured: 1,
  },
  {
    name: 'research-lab', displayName: 'Research Lab', author: 'kirocrew',
    description: 'A builtin whose absolute hero path must pass through untouched.',
    tags: ['research'], version: '1.4.0', installed: true, enabled: true,
    origin: 'builtin', provenance: 'builtin', verified: true, featured: 2,
    heroImage: '/app-assets/research-lab/hero.svg',
    heroImageDark: '/app-assets/research-lab/hero-dark.svg',
  },
]

const installedApps = [
  {
    name: 'pixel-pal', displayName: 'Pixel Pal', version: '1.0.0', enabled: true,
    installedAt: '2026-07-20T10:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name: 'pixel-pal', version: '1.0.0', displayName: 'Pixel Pal',
      description: 'A registry app whose manifest declares repo-relative hero art.',
      author: 'octocat', tags: ['images'], repo: REPO,
      // The #1255 shape: repo-relative art paths straight from app.json.
      heroImage: 'assets/hero.svg', heroImageDark: 'assets/hero-dark.svg',
      screenshots: ['assets/shot-1.svg'], iconPath: 'assets/icon.svg',
    },
  },
  {
    name: 'research-lab', displayName: 'Research Lab', version: '1.4.0', enabled: true,
    installedAt: '2026-07-20T10:00:00Z', origin: 'builtin', resources: 'gateway', lifecycle: 'locked',
    manifest: {
      name: 'research-lab', version: '1.4.0', displayName: 'Research Lab',
      description: 'A builtin whose absolute hero path must pass through untouched.',
      author: 'kirocrew', tags: ['research'],
      heroImage: '/app-assets/research-lab/hero.svg',
      heroImageDark: '/app-assets/research-lab/hero-dark.svg',
      ui: { pages: [{ route: '/research', label: 'Research', icon: 'Search' }] },
    },
  },
]

const art = (from, to, label) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 675"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${from}"/><stop offset="1" stop-color="${to}"/></linearGradient></defs><rect width="1200" height="675" fill="url(#g)"/><circle cx="980" cy="150" r="270" fill="#fff" opacity=".09"/><text x="64" y="600" font-family="Helvetica,Arial" font-size="60" font-weight="700" fill="#fff" opacity=".92">${label}</text></svg>`

const browser = await chromium.launch()
// The built SPA registers a service worker whose fetch handler would bypass
// page.route for non-/api requests (the /app-assets fixture) — block it.
const context = await browser.newContext({ viewport: { width: 1520, height: 1000 }, deviceScaleFactor: 2, serviceWorkers: 'block' })
const page = await context.newPage()

let themeMode = 'dark'
let wsServer = null
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

// Records every blob-proxy hit so the harness can assert the exact repo+path.
const blobHits = []
await page.route('**/app-assets/**', route => {
  const p = new URL(route.request().url()).pathname
  const dark = p.includes('dark')
  return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: art(dark ? '#0c3742' : '#b9e6f2', '#22d3ee', 'Research Lab') })
})
// Fixture responses keyed by exact path; functions are lazy so the theme-boot
// entry can read the mutable themeMode. (A lookup table rather than the
// if-chain of the sibling capture scripts — same behavior, and keeps the
// jscpd duplication gate quiet.)
const API_FIXTURES = {
  '/api/apps/registry': () => ({ apps: registryApps, serverPlatform: { os: 'darwin', arch: 'arm64' } }),
  '/api/apps/registries': () => ({ registries: [{ name: REG, repo: 'https://github.com/kirodotdev-labs/app-registry', branch: 'main' }] }),
  '/api/apps': () => installedApps,
  '/api/auth/me': () => ({ user: 'owner', app: '' }),
  '/api/kiro-prerequisite': () => ({
    platform: 'gateway', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: true,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { status: 'idle', message: '' },
  }),
  '/api/themes': () => ({ themes: [], installed: [] }),
  '/api/status': () => status,
  '/api/dashboard/branding': () => ({ bot_name: 'Kiro Crew', avatar: '' }),
  '/api/theme/boot': () => ({ mode: themeMode, theme: '' }),
  '/api/notifications': () => ({ notifications: [], unread: 0 }),
  '/api/chat/slots': () => [],
  '/api/models': () => ({ models: [], default: 'auto' }),
}
await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  if (path === '/api/apps/blob') {
    const repo = url.searchParams.get('repo')
    const blobPath = url.searchParams.get('path')
    blobHits.push({ repo, path: blobPath })
    if (repo !== REPO) return route.fulfill({ status: 404, body: '' })
    const dark = (blobPath || '').includes('dark')
    return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: art(dark ? '#2e1f57' : '#cfc1ff', '#6d4aff', 'Pixel Pal') })
  }
  const fixture = API_FIXTURES[path]
  if (fixture) return json(route, fixture())
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  const objectish = /(config|tips|voice|autonudge|branding|status|themes)/.test(path)
  return json(route, objectish ? {} : [])
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  // Guarded: the light-theme capture flips this key before reloading.
  if (!localStorage.getItem('mc-theme-mode')) localStorage.setItem('mc-theme-mode', 'dark')
})

const pushStatus = () => wsServer && wsServer.send(JSON.stringify({ type: 'status', data: status }))
async function settle(ms = 1600) { await page.waitForTimeout(ms); pushStatus(); await page.waitForTimeout(600) }

const PROXIED_DARK = `/api/apps/blob?repo=${encodeURIComponent(REPO)}&path=${encodeURIComponent('assets/hero-dark.svg')}`
const PROXIED_LIGHT = `/api/apps/blob?repo=${encodeURIComponent(REPO)}&path=${encodeURIComponent('assets/hero.svg')}`

function fail(msg) { throw new Error(`ASSERTION FAILED: ${msg}`) }

/** Assert an <img> with the exact src exists and has actually loaded. */
async function assertHero(label, src) {
  const img = page.locator(`img[src="${src}"]`).first()
  if (await img.count() === 0) fail(`${label}: no <img> with src ${src}`)
  const loaded = await img.evaluate(el => el.complete && el.naturalWidth > 0)
  if (!loaded) fail(`${label}: hero <img> ${src} did not load`)
  console.log(`OK ${label}: ${src}`)
}

// ---- Discover (landing, dark): merged registry row resolves through the proxy
await page.goto(`http://127.0.0.1:${PORT}/apps`, { waitUntil: 'domcontentloaded' })
await settle(2400)
await assertHero('discover relative→proxy', PROXIED_DARK)
await assertHero('discover absolute untouched', '/app-assets/research-lab/hero-dark.svg')
await page.screenshot({ path: `${OUT}/discover-dark.png` })

// ---- Library (dark): raw installed manifest resolves through the proxy
await page.getByText('Library').first().click()
await settle(1400)
await assertHero('library relative→proxy', PROXIED_DARK)
await assertHero('library absolute untouched', '/app-assets/research-lab/hero-dark.svg')
await page.screenshot({ path: `${OUT}/library-dark.png` })

if (!blobHits.some(h => h.repo === REPO && h.path === 'assets/hero-dark.svg')) {
  fail(`blob proxy never queried with repo=${REPO} path=assets/hero-dark.svg — hits: ${JSON.stringify(blobHits)}`)
}
console.log('OK blob proxy hits:', JSON.stringify(blobHits))

// ---- Library (light): theme flip prefers heroImage, still proxied
themeMode = 'light'
await page.evaluate(() => localStorage.setItem('mc-theme-mode', 'light'))
await page.goto(`http://127.0.0.1:${PORT}/apps`, { waitUntil: 'domcontentloaded' })
await settle(2400)
await page.getByText('Library').first().click()
await settle(1400)
await assertHero('library light relative→proxy', PROXIED_LIGHT)
await page.screenshot({ path: `${OUT}/library-light.png` })

await context.close()
await browser.close()
server.close()
console.log('done')
