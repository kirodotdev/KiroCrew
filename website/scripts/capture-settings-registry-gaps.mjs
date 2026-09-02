/**
 * Screenshot harness for the settings-search REGISTRY GAP fixes.
 *
 * Photographs, against the real built SPA (website/dist) with the dashboard
 * API stubbed, the searches that returned NOTHING before this change:
 *
 *  1. "custom denies"  — SecurityPanel's add-your-own-deny-patterns card
 *                        (bare-control composite, new settingsManual entry)
 *  2. "bot token"      — SecretField credential rows across channel panels
 *                        (the extractor now indexes SecretField/TagListEditor)
 *  3. "approval sound" — NotificationsPanel per-category sound selects
 *                        (dynamic labels, new settingsManual entries)
 *  4. "who can message" — WhatsApp/WeChat DM-policy selects migrated from
 *                        bare SimpleSelect to SettingsSelect
 *  5. The deep link landed: activating the "custom denies" hit mounts
 *                        Security ?section=rules and rings the card's new
 *                        data-setting-label anchor.
 *
 * Every scene asserts its expected row is present BEFORE shooting, so the
 * evidence cannot photograph an empty dropdown.
 *
 * Usage: node scripts/capture-settings-registry-gaps.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/settings-search-registry/search'
const DIST = fileURLToPath(new URL('../dist/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

function serveDist() {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '')
      let file = join(DIST, rel)
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
  deviceScaleFactor: 2,
})
const page = await context.newPage()

await page.routeWebSocket(/\/api\/ws/, () => {})

await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/chat/slots') return json(route, [])
  // Scene 5 lands on the Security tab, whose sections read named fields —
  // the generic fallback's `[]`/`{}` shapes error-boundary the page.
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

const input = page.getByRole('combobox', { name: 'Search settings' })
await input.waitFor({ state: 'visible', timeout: 15_000 })
await page.waitForTimeout(600)

/** Type a query, assert some option contains `expect`, screenshot. */
async function scene(name, query, expected) {
  await input.fill(query)
  await page.getByRole('listbox').waitFor({ state: 'visible', timeout: 5_000 })
  const texts = await page.getByRole('option').allTextContents()
  const missing = expected.filter(e => !texts.some(t => t.includes(e)))
  if (missing.length) {
    throw new Error(`scene ${name}: query ${JSON.stringify(query)} missing expected row(s) ${JSON.stringify(missing)} in ${JSON.stringify(texts)}`)
  }
  await page.screenshot({ path: join(OUT, `${name}.png`), fullPage: false })
  console.log(`captured ${name}.png (${expected.length} expected row(s) asserted)`)
}

await scene('1-search-custom-denies', 'custom denies', ['Your custom denies'])
// Discord/Telegram/WeCom token labels arrive via BotChannelSpec props (dynamic,
// accounted in EXPECTED_DYNAMIC_SKIPS) — the statically-labelled SecretFields
// are what the extractor indexes.
await scene('2-search-bot-token', 'bot token', ['Slack bot token', 'Webex bot token'])
await scene('3-search-approval-sound', 'approval', ['Tool approval requests'])
await scene('4-search-who-can-message', 'who can message', ['(WhatsApp)', '(WeChat)'])

// ── Scene 5: activate "custom denies" → deep link + highlight ring ─────────
await input.fill('custom denies')
await page.getByRole('listbox').waitFor({ state: 'visible', timeout: 5_000 })
const topRow = page.getByRole('option').first()
await topRow.dispatchEvent('mousedown')
const ringed = page.locator('[data-setting-label][style*="outline"]').first()
await ringed.waitFor({ state: 'visible', timeout: 15_000 })
await page.screenshot({ path: join(OUT, '5-deep-link-custom-denies.png'), fullPage: false })
console.log('captured 5-deep-link-custom-denies.png — ringed:', await ringed.getAttribute('data-setting-label'))

await browser.close()
srv.close()
