/**
 * Screenshot harness for Knowledge-inside-Agent-Capabilities.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback, and answers every /api/** call from fixtures via Playwright
 * route interception. No gateway, no dashboard auth, no kiro-cli spawn.
 *
 * Two shots:
 *   1. knowledge-tab.png    — loaded via the OLD /knowledge URL, so the frame
 *                             proves the redirect AND the grouped rail with the
 *                             Knowledge tab active in one image.
 *   2. grouped-rail.png     — the default Capabilities view (Crews), showing
 *                             the three group headers at rest.
 *
 * Usage: node scripts/capture-capabilities-knowledge.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/capabilities-knowledge-shots'
const DIST = fileURLToPath(new URL('../dist/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

/** Static server with index.html fallback so /knowledge deep-links resolve. */
function serveDist() {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '')
      let file = join(DIST, rel)
      if (!rel || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, 'index.html')
      res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' })
      res.end(readFileSync(file))
    })
    srv.listen(0, '127.0.0.1', () => resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }))
  })
}

const SOURCES = [
  { id: 's1', name: 'notes', source_type: 'local_folder', uri: '/home/user/notes', sync_status: 'synced', item_count: 24 },
  { id: 's2', name: 'Uploads', source_type: 'upload', uri: 'upload://', sync_status: 'synced', item_count: 8 },
  { id: 's3', name: 'Web pages', source_type: 'web', uri: 'web://', sync_status: 'synced', item_count: 3 },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()

// Feature fixtures only; boot-path endpoints come from the shared stub.
const extra = async (path, route) => {
  if (path === '/api/knowledge/source-counts') { await json(route, { counts: { s1: 24, s2: 8, s3: 3 }, total: 35 }); return true }
  if (path === '/api/knowledge/sources') { await json(route, SOURCES); return true }
  if (path === '/api/knowledge/stats') { await json(route, { items: 35, entities: 120, relations: 88, sources: 3 }); return true }
  if (path === '/api/knowledge/namespaces') { await json(route, [{ name: 'default', count: 35 }]); return true }
  if (path === '/api/knowledge/config') { await json(route, { enabled: true, supported_formats: ['.md', '.txt', '.pdf'] }); return true }
  if (path.startsWith('/api/knowledge/items')) { await json(route, { items: [], total: 0 }); return true }
  if (path === '/api/models') { await json(route, [{ model_name: 'auto', description: 'Let Kiro choose' }]); return true }
  return false
}
logPageProblems(page)
await stubDashboardApi(page, { extra })

// Shot 1: enter through the OLD URL — the frame only shows the Knowledge tab
// if the /knowledge -> /capabilities?tab=knowledge redirect actually fired.
await page.goto(base + '/knowledge', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await page.screenshot({ path: join(OUT, 'knowledge-tab.png') })
console.log('shot knowledge-tab.png  url =', page.url())

// Shot 2: the default Capabilities view, grouped rail at rest. The remembered
// tab (SidePanelLayout rememberKey) must be cleared first or shot 1's
// selection sticks and this frame re-renders the Knowledge tab.
await page.evaluate(() => sessionStorage.clear())
await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2400)
await page.screenshot({ path: join(OUT, 'grouped-rail.png') })
console.log('shot grouped-rail.png  url =', page.url())

await browser.close()
srv.close()
