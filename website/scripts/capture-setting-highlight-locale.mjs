/**
 * Screenshot harness for the locale-independent settings highlight.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback, and answers every /api/** call from fixtures via Playwright
 * route interception. No gateway, no dashboard auth, no kiro-cli spawn.
 *
 * The dashboard is put into Japanese, then opened on the deep link a curated tip
 * ships: `/settings?tab=display&highlight=display.language`. The registry entry
 * for that row carries no `configKey`, so the label-derived id is its only
 * route — which is exactly the anchor that could not resolve while the hook
 * matched the English label against a translated `data-setting-label`.
 *
 * The highlight is a 2s ring that then fades, so the shot is taken while it is
 * still up rather than after any fixed sleep.
 *
 * Usage: node scripts/capture-setting-highlight-locale.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '/tmp/setting-highlight-locale-shots'
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
      if (!rel || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, 'index.html')
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
  // Nothing scene-specific: the Display panel renders from the shell's own boot
  // calls, and the shared fixtures already answer those in the shapes the SPA
  // consumes (several are shape-sensitive — a bare {} where a list is expected
  // takes the whole page down through the error boundary).
  if (path === '/api/chat/slots') return json(route, [])
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
  // The dashboard in Japanese — the condition the anchor could not survive.
  localStorage.setItem('mc-lang', 'ja')
})

await page.goto(`${base}/settings?tab=display&highlight=display.language`, {
  waitUntil: 'domcontentloaded',
})

// The hook waits a tick for the panel, then rings the row for 2s. Wait for the
// ring itself rather than a fixed sleep, so the shot cannot race the flash.
//
// `SHOT` names which half of the pair is being taken. The "before" run is
// pointed at a build without the fix, where no ring ever appears — so that mode
// waits out the window and photographs the absence instead of failing.
const SHOT = process.env.HIGHLIGHT_SHOT === 'before' ? 'before' : 'after'
const ringed = page.locator('[data-setting-label][style*="outline"]').first()

if (SHOT === 'after') {
  await ringed.waitFor({ state: 'visible', timeout: 15_000 })
} else {
  // INSIDE the window a highlight would occupy: past the hook's 100ms settle,
  // well before it clears the ring at ~2.3s. Waiting longer would photograph a
  // faded ring and call it an absence — the shot has to be taken while a
  // working build would still be showing one.
  await page.waitForTimeout(900)
  if (await ringed.count()) {
    throw new Error('before-shot expected no highlight, but one was applied')
  }
}

const file = SHOT === 'before'
  ? 'before-japanese-no-highlight.png'
  : 'after-japanese-highlight.png'
await page.screenshot({ path: join(OUT, file), fullPage: false })
console.log(`captured ${file}`)
if (SHOT === 'after') console.log('ringed row label:', await ringed.getAttribute('data-setting-label'))

await browser.close()
srv.close()
