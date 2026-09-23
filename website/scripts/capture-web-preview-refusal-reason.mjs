/**
 * Recording harness for the Web Preview panel's refusal card on an https-served
 * dashboard (#10696): the explanation must follow the BLOCKER, not the scheme.
 *
 * Runs the REAL built SPA (website/dist) gateway-free, with every /api/** call
 * answered from fixtures. The dashboard is reached at an `https://` origin —
 * which is the only deployment where the card exists — without any TLS
 * listener: a Playwright route rewrites `https://<host>/…` onto the plain
 * static server, so the document's `location.protocol` is genuinely `https:`
 * while no certificate is ever involved.
 *
 * Two frames per theme, one per branch of the card:
 *
 *  - `*.localhost`  — potentially trustworthy, so the engine allows it; the
 *                     dashboard's own CSP `frame-src` is what refuses it. The
 *                     card must name the gateway policy.
 *  - `0.0.0.0`      — not loopback, so the engine really blocks it as mixed
 *                     content. The card keeps the browser wording.
 *
 * The probes assert which branch rendered (by test id) and that the
 * open-in-browser escape hatch survives on both, so the PNGs cannot be frames
 * of the wrong surface.
 *
 * Usage: node scripts/capture-web-preview-refusal-reason.mjs [outDir] [dark|light]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/web-preview-refusal-reason'
const THEME = process.argv[3] || 'dark'
const SLOT = 's-preview'
// Any https origin will do: it never resolves, the route below answers for it.
const HTTPS_ORIGIN = 'https://dashboard.example.test'

mkdirSync(OUT, { recursive: true })

// Generic titles: these strings land in a public PR, so they must not name
// anything from a private codebase.
const NOW = Date.parse('2026-09-15T18:00:00Z')
const SEED = [
  { key: SLOT, title: 'Checkout flow — responsive pass', hoursAgo: 0.02 },
  { key: 's2', title: 'Empty states for the settings pages', hoursAgo: 2 },
  { key: 's3', title: 'Token audit across the theme files', hoursAgo: 5 },
]
const slots = SEED.map(s => ({
  key: s.key,
  title: s.title,
  running: false,
  last_message: '',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  created: '2026-09-01T01:00:00Z',
  last_ts: new Date(NOW - s.hoursAgo * 3600_000).toISOString(),
  modified: Math.floor((NOW - s.hoursAgo * 3600_000) / 1000),
  folder_id: '',
  source_links: [],
  source_links_total: 0,
}))

const SCENES = [
  { id: 'policy', url: 'http://myapp.localhost:5173/', expectReason: 'policy' },
  { id: 'browser', url: 'http://0.0.0.0:5173/', expectReason: 'browser' },
]

/** Serve the built SPA at an https origin by rewriting each request onto the
 *  plain static server. Registered FIRST so the API stubs (registered later)
 *  take precedence — Playwright resolves routes last-registered-wins. */
async function serveOverHttps(page, httpBase) {
  await page.route(`${HTTPS_ORIGIN}/**`, async route => {
    const u = new URL(route.request().url())
    try {
      const response = await route.fetch({ url: httpBase + u.pathname + u.search })
      await route.fulfill({ response })
    } catch {
      // A lazy chunk still in flight when the scene's page closes: nothing to
      // serve it to any more, and not a finding about the surface.
      await route.abort().catch(() => {})
    }
  })
}

async function boot(context, httpBase, previewUrl) {
  const page = await context.newPage()
  await serveOverHttps(page, httpBase)
  // Seeds go through the stub's own init script, after its clear: Playwright
  // does not define the evaluation order of separately registered init
  // scripts, so a second addInitScript would race that clear.
  await stubDashboardApi(page, {
    slots,
    theme: THEME,
    localStorageEntries: {
      'mc-active-slot': SLOT,
      ['mc-activity-open:' + SLOT]: 'true',
      'mc-privacy-notice-v1': '1',
      'mc-lang': 'en',
      ['mc-panel-tabs:' + SLOT]: JSON.stringify({
        activeId: 'browser', tabs: [{ id: 'browser', kind: 'browser' }],
      }),
      ['mc-webpreview-url:' + SLOT]: previewUrl,
    },
  })
  logPageProblems(page)
  await page.goto(HTTPS_ORIGIN + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  return page
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  })

  const probe = {}
  for (const scene of SCENES) {
    const page = await boot(context, base, scene.url)
    const card = page.getByTestId(`web-preview-embed-refusal-${scene.expectReason}`)
    // Fail the capture rather than shipping a frame of the wrong surface.
    await card.waitFor({ state: 'visible', timeout: 15000 })
    const otherReason = scene.expectReason === 'policy' ? 'browser' : 'policy'
    probe[scene.id] = {
      servedOverHttps: await page.evaluate(() => location.protocol) === 'https:',
      reasonRendered: (await card.count()) === 1,
      otherReasonAbsent: (await page.getByTestId(`web-preview-embed-refusal-${otherReason}`).count()) === 0,
      noFrame: (await page.locator('iframe[title="Web preview"]').count()) === 0,
      // The card's own link, not the toolbar's: `xpath=..` climbs to the card.
      openInBrowserHref:
        (await card.locator('xpath=..').getByRole('link', { name: 'Open in browser' })
          .getAttribute('href')) === scene.url,
      text: (await card.textContent())?.trim(),
    }
    await page.screenshot({ path: `${OUT}/${THEME}-${scene.id}.png` })
    await page.close()
  }
  console.log(`PROBE ${THEME} ${JSON.stringify(probe, null, 2)}`)

  const failed = Object.entries(probe)
    .flatMap(([scene, checks]) => Object.entries(checks)
      .filter(([, v]) => v === false)
      .map(([k]) => `${scene}.${k}`))
  await context.close()
  await browser.close()
  srv.close()
  if (failed.length) throw new Error(`probe failed: ${failed.join(', ')}`)
  console.log('wrote frames to', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
