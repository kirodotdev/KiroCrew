/**
 * Screenshot harness for the in-page settings search.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback, and answers every /api/** call from fixtures via Playwright
 * route interception. No gateway, no dashboard auth, no kiro-cli spawn.
 *
 * Three shots, matching the PR's evidence set:
 *  1. The search box in the Settings header, dropdown open on a query.
 *  2. The flagship "yolo" query — the TOP row must be the auto-approve
 *     duration entry (keyword rank), not a scattered-subsequence label hit;
 *     the harness asserts this before shooting, so the evidence cannot
 *     photograph the ranking bug again.
 *  3. The deep link landed: Security tab, section=approval mounted, target
 *     row ringed by useSettingHighlight.
 *
 * Usage: node scripts/capture-settings-search.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '/tmp/settings-search-shots'
// fileURLToPath, not URL.pathname: on Windows .pathname yields "/C:/…", which
// join() then turns into an invalid "\C:\…" and every read fails with ENOENT.
const DIST = fileURLToPath(new URL('../dist/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

/** Static server with index.html fallback so /settings deep-links resolve. */
function serveDist() {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '')
      let file = join(DIST, rel)
      // Loopback-only and ephemeral, but keep the join honest anyway: an
      // encoded ../ must not escape the dist root.
      if (!file.startsWith(DIST) || !rel || !existsSync(file) || statSync(file).isDirectory()) {
        file = join(DIST, 'index.html')
      }
      res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' })
      res.end(readFileSync(file))
    })
    srv.listen(0, '127.0.0.1', () => resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }))
  })
}

const PROJECT = '/home/user/project'
const fixedApi = makeFixedApi(PROJECT)

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1400, height: 900 },
  // Settings rows are 12–13px type; a 1x shot renders soft on GitHub.
  deviceScaleFactor: 2,
})
const page = await context.newPage()

await page.routeWebSocket(/\/api\/ws/, () => {})

await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  // Nothing scene-specific for the Chat tab; shot 3 lands on the Security tab,
  // whose sections are shape-sensitive — the shared fallback answers `[]`/`{}`
  // and SecurityPanel reads named fields off those, error-boundarying the
  // whole page (`undefined.filter`).
  if (path === '/api/chat/slots') return json(route, [])
  if (path === '/api/security/denied-commands') {
    return json(route, { builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false })
  }
  if (path === '/api/security/posture') return json(route, { controls: [], counts: {} })
  if (path === '/api/governance/policy') {
    return json(route, { version: null, has_policy: false, profile: null, scopes: [] })
  }
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
})

await page.goto(`${base}/settings?tab=chat`, { waitUntil: 'domcontentloaded' })

// By accessible name, not bare role: the settings panels' own selects expose
// role=combobox too (15 on the Chat tab alone), so a role-only locator is
// ambiguous under strict mode.
const input = page.getByRole('combobox', { name: 'Search settings' })
await input.waitFor({ state: 'visible', timeout: 15_000 })
// Panels finish their fixture fetches well inside this; a settling wait keeps
// the background out of any loading skeleton state in the shot.
await page.waitForTimeout(600)

// ── Shot 1: the search box open on a broad query ───────────────────────────
await input.fill('model')
await page.getByRole('listbox').waitFor({ state: 'visible', timeout: 5_000 })
await page.screenshot({ path: join(OUT, '1-settings-header-search.png'), fullPage: false })
console.log('captured 1-settings-header-search.png')

// ── Shot 2: the flagship "yolo" query, ranking asserted before shooting ────
await input.fill('yolo')
await page.getByRole('listbox').waitFor({ state: 'visible', timeout: 5_000 })
const topRow = page.getByRole('option').first()
const topText = await topRow.textContent()
if (!topText?.includes('How long auto-approve stays on')) {
  throw new Error(`ranking regression: top row for "yolo" is ${JSON.stringify(topText)}, expected the auto-approve duration entry`)
}
await page.screenshot({ path: join(OUT, '2-search-yolo-results.png'), fullPage: false })
console.log('captured 2-search-yolo-results.png (top row asserted)')

// ── Shot 3: activate the top row → deep link + highlight ring ──────────────
await topRow.dispatchEvent('mousedown')
// The hook waits a tick for the panel, then rings the row for 2s. Wait for the
// ring itself rather than a fixed sleep, so the shot cannot race the flash.
const ringed = page.locator('[data-setting-label][style*="outline"]').first()
await ringed.waitFor({ state: 'visible', timeout: 15_000 })
await page.screenshot({ path: join(OUT, '3-deep-link-highlight.png'), fullPage: false })
console.log('captured 3-deep-link-highlight.png')
console.log('ringed row label:', await ringed.getAttribute('data-setting-label'))

await browser.close()
srv.close()
