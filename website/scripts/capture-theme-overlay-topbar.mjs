/**
 * Screenshot + stacking probe for #7377: a theme pack's `fullscreen` decorative
 * overlay must paint UNDER the dashboard top bar, in every window state.
 *
 * Why a probe and not just a frame: the header is glass (74% chrome + blur), so
 * a translucent overlay bleeding through it and an overlay painted OVER it can
 * look alike in a still. Each frame is therefore paired with a hit-test: the
 * overlay iframe is `pointer-events: none` by contract, so the probe flips it to
 * `auto` for one `elementFromPoint` call at the header's centre and flips it
 * back. Whatever the browser returns is the element that actually paints on
 * top -- the same order the compositor uses -- with no CSS reasoning involved.
 *
 * The pack is a fixture served through the API stub: an L2 theme whose single
 * overlay is a full-bleed opaque stripe field asking for the maximum zIndex the
 * frontend clamp allows. Nothing is mocked inside the app -- `useTheme` loads it
 * through the real `/api/themes` path and `ThemeExperienceLayer` mounts the
 * real overlay iframe.
 *
 * Frames (top strip only, 2x):
 *   normal.png   default shell, header in the grid
 *   focus.png    focus mode, header peeked (the `absolute` overlay path that
 *                stands in for the macOS-fullscreen stacking change the report
 *                describes; no macOS here)
 *
 * Exit status is non-zero when the header loses the hit-test in either state,
 * so the same script is the regression check and the evidence.
 *
 * Usage: node scripts/capture-theme-overlay-topbar.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/theme-overlay-topbar'
mkdirSync(OUT, { recursive: true })

const SLUG = 'stripe-probe'
const OVERLAY_ID = 'stripes'
const STRIP = { x: 0, y: 0, width: 1280, height: 64 }

/** The overlay document: opaque, high-contrast, animated -- impossible to miss. */
const OVERLAY_HTML = `<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:transparent}
  body::before{content:'';position:fixed;inset:0;
    background:repeating-linear-gradient(135deg,#ff3ea5 0 18px,#1b1b1b 18px 36px);
    animation:slide 1.2s linear infinite}
  @keyframes slide{to{background-position:51px 0}}
</style></head><body></body></html>`

const THEME_ROW = { slug: SLUG, name: 'Stripe Probe', emoji: '📼', source: 'installed' }
const THEME_DETAIL = {
  name: 'Stripe Probe', slug: SLUG, emoji: '📼', level: 2, dark: {}, light: {},
  assets: {
    overlays: [{
      id: OVERLAY_ID, position: 'fullscreen', zIndex: 9999,
      pointerEvents: false, animation: 'continuous', trigger: 'continuous',
    }],
  },
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
logPageProblems(page)

await stubDashboardApi(page, {
  slots: [{
    key: 'chat-1', title: 'Theme overlay probe', running: false,
    last_message: 'Checking the top bar stays above the theme.', messages: 2,
    agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
    modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
  }],
  localStorageEntries: { 'mc-color-theme': `custom-${SLUG}` },
  extra: async (path, route) => {
    const ok = (body, type = 'application/json') =>
      route.fulfill({ status: 200, contentType: type, body: typeof body === 'string' ? body : JSON.stringify(body) })
    if (path === '/api/themes') { await ok({ themes: [THEME_ROW], installed: [SLUG] }); return true }
    if (path === `/api/themes/${SLUG}`) { await ok(THEME_DETAIL); return true }
    if (path === '/api/theme/boot') { await ok({ mode: 'dark', color: `custom-${SLUG}` }); return true }
    if (path === `/api/theme/${SLUG}/overlay/${OVERLAY_ID}`) { await ok(OVERLAY_HTML, 'text/html'); return true }
    return false
  },
})

await page.goto(`${base}/`)
await page.waitForSelector('header.topbar')
const overlay = page.locator(`iframe[data-theme-frame="1"][title*="${OVERLAY_ID}"]`)
await overlay.waitFor({ state: 'attached', timeout: 15_000 })
// Let the iframe document load and its first animation frame paint.
await page.waitForTimeout(600)

/**
 * Who paints on top at the header's centre: 'header' or 'overlay'. Flips the
 * overlay's pointer-events for the duration of ONE hit-test only.
 */
async function topmostAtHeaderCentre() {
  return page.evaluate(() => {
    const header = document.querySelector('header.topbar')
    const frame = document.querySelector('iframe[data-theme-frame="1"]')
    if (!header || !frame) return 'missing'
    const r = header.getBoundingClientRect()
    const prev = frame.style.pointerEvents
    frame.style.pointerEvents = 'auto'
    try {
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      if (hit === frame) return 'overlay'
      return header.contains(hit) ? 'header' : `other:${hit?.tagName?.toLowerCase() ?? 'null'}`
    } finally {
      frame.style.pointerEvents = prev
    }
  })
}

const readZ = async () => page.evaluate(() => {
  const header = document.querySelector('header.topbar')
  const frame = document.querySelector('iframe[data-theme-frame="1"]')
  return {
    header: getComputedStyle(header).zIndex,
    overlay: getComputedStyle(frame).zIndex,
    overlayParent: frame.parentElement?.getAttribute('data-testid') || frame.parentElement?.id || frame.parentElement?.tagName,
  }
})

const results = {}

// ── normal shell ──
results.normal = { z: await readZ(), top: await topmostAtHeaderCentre() }
await page.screenshot({ path: `${OUT}/normal.png`, clip: STRIP })
console.log('normal:', JSON.stringify(results.normal))

// ── focus mode: the header leaves the grid and becomes an absolute overlay ──
await page.getByRole('button', { name: /focus mode/i }).first().click()
// Peek the top strip so the (now hidden) header slides back into view.
await page.mouse.move(640, 1)
await page.waitForFunction(() => {
  const h = document.querySelector('header.topbar')
  return h && getComputedStyle(h).transform === 'none' || /matrix\(1, 0, 0, 1, 0, 0\)/.test(getComputedStyle(h).transform)
}, null, { timeout: 5_000 })
await page.waitForTimeout(350)
results.focus = { z: await readZ(), top: await topmostAtHeaderCentre() }
await page.screenshot({ path: `${OUT}/focus.png`, clip: STRIP })
console.log('focus:', JSON.stringify(results.focus))

await browser.close()
srv.close()

const losers = Object.entries(results).filter(([, r]) => r.top !== 'header').map(([k]) => k)
if (losers.length) {
  console.log(`FAIL: overlay paints over the top bar in: ${losers.join(', ')}`)
  process.exit(1)
}
console.log('PASS: top bar paints over the theme overlay in every state')
