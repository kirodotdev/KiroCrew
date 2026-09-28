/**
 * Screenshot + computed-token probe for the theme-pack palette gap-fill.
 *
 * `variables.json` requires only `--bg`, `--text` and `--accent`, so a valid
 * pack may declare three of the 56 allowlisted tokens. Everything it omits
 * inherits from `index.css`'s bare `:root`, and that selector carries the DARK
 * palette with no light counterpart — so a pack with a light `--bg` used to
 * paint dark-mode secondary text onto its own light surfaces.
 *
 * Why a probe and not just a frame: the worst token here is `--muted-fg`, which
 * inherited `#fff` and therefore rendered white-on-white. A control that is
 * invisible and a control that is absent photograph identically, so a still
 * cannot tell the bug from a layout change. Each frame is paired with a read of
 * the REAL computed custom properties off `document.documentElement`, plus the
 * WCAG contrast that value actually achieves against the pack's own `--card`.
 * That number is the evidence; the frame is the illustration.
 *
 * The pack is a fixture served through the API stub: a level-0 theme declaring
 * exactly the documented minimum, with a light `light` block. Nothing is mocked
 * inside the app — `useTheme` loads it through the real `/api/themes` path and
 * `buildCustomThemeCss` emits the block under test.
 *
 * Frames (2x):
 *   <label>-settings-display.png   Settings → Display, the densest run of
 *                                  secondary text and segmented controls
 *   <label>-session-list.png       the session rail, where muted titles live
 *
 * Run it twice to get a pair, rebuilding `website/dist` in between:
 *   git checkout HEAD~1 -- src/hooks/themeCss.ts && npm run build
 *   node scripts/capture-theme-pack-token-fallback.mjs ../temp-screenshots/theme-pack-token-fallback before
 *   git checkout HEAD -- src/hooks/themeCss.ts && npm run build
 *   node scripts/capture-theme-pack-token-fallback.mjs ../temp-screenshots/theme-pack-token-fallback after
 *
 * With label `after` the probe is also the regression check: it exits non-zero
 * unless every measured token clears WCAG AA on the pack's own surface.
 *
 * Usage: node scripts/capture-theme-pack-token-fallback.mjs [outDir] [before|after]
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/theme-pack-token-fallback'
const LABEL = process.argv[3] || 'after'
if (LABEL !== 'before' && LABEL !== 'after') {
  throw new Error(`label must be "before" or "after", got ${LABEL}`)
}
mkdirSync(OUT, { recursive: true })

const SLUG = 'minimal-light'
/**
 * The documented minimum and nothing else. Any extra key here would mask the
 * defect, so the fixture is deliberately as sparse as install allows.
 */
const THEME_DETAIL = {
  name: 'Minimal Light', slug: SLUG, emoji: '🧪', level: 0,
  dark: { '--bg': '#060e2a', '--text': '#d4e6ff', '--accent': '#f4d03f' },
  light: { '--bg': '#e8f8f5', '--text': '#1a3a4a', '--accent': '#e67e22' },
  assets: {},
}
const THEME_ROW = { slug: SLUG, name: THEME_DETAIL.name, emoji: THEME_DETAIL.emoji, source: 'installed' }

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
  if (path === '/api/themes') return json(route, { themes: [THEME_ROW], installed: [SLUG] })
  if (path === `/api/themes/${SLUG}`) return json(route, THEME_DETAIL)
  if (path === '/api/theme/boot') return json(route, { mode: 'light', color: `custom-${SLUG}` })
  if (path === '/api/chat/slots') {
    return json(route, [{
      key: 'chat-1', title: 'Reviewing the light palette', running: false,
      last_message: 'Secondary text lives in the session rail too.', messages: 4,
      agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
      modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
    }])
  }
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(slug => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'light')
  localStorage.setItem('mc-color-theme', `custom-${slug}`)
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
}, SLUG)

// Settings cards carry `animate-rise` AND `transition-all`, so a colour read too
// early returns an interpolated `oklab(...)` rather than the settled token. That
// is not a theory: an earlier run of this probe reported the SAME
// `oklab(0.222159 …)` for a custom pack and for the built-in light theme, which
// is only possible if both were mid-transition from the same starting colour.
// Freeze motion so the frame and the numbers describe the resting state.
await page.emulateMedia({ reducedMotion: 'reduce' })
const freezeMotion = (p) => p.addStyleTag({
  content: '*,*::before,*::after{transition:none!important;animation:none!important}',
}).catch(() => {})

/**
 * Measure what is actually PAINTED, not what the tokens say.
 *
 * An earlier version of this probe resolved each token string on a detached
 * element. Chromium does not resolve `color-mix()` that way, so every derived
 * token came back unparsed and two different colours scored 1:1 — a green
 * reading that meant nothing. Reading real elements avoids the whole question:
 * `getComputedStyle().color` on a rendered node is always `rgb()`, and the
 * effective background is found by walking ancestors until one is opaque, the
 * same thing the compositor does.
 */
const PROBE = () => {
  // Resolve ANY computed colour format to 8-bit sRGB by painting it. Parsing the
  // string does not work here: Tailwind builds its alpha utilities with
  // `color-mix(in oklab, …)`, so computed values come back as `oklab(...)` or as
  // `rgb()` with 0–1 fractional channels, and a naive numeric parse reads 0.23 as
  // near-black. The canvas does the conversion the compositor would.
  const cv = document.createElement('canvas')
  cv.width = cv.height = 1
  const ctx = cv.getContext('2d', { willReadFrequently: true })
  const paint = (css) => {
    if (!css || css === 'transparent' || css === 'rgba(0, 0, 0, 0)') return null
    ctx.clearRect(0, 0, 1, 1)
    ctx.fillStyle = '#000'
    ctx.fillStyle = css
    // An unparseable value leaves fillStyle at the previous colour, so a value
    // the browser rejected is reported as such instead of silently scoring black.
    if (ctx.fillStyle === '#000' && !/^(#000000?|black|rgb\(0,\s*0,\s*0\))$/i.test(css.trim())) return null
    ctx.fillRect(0, 0, 1, 1)
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data
    return a < 240 ? null : [r, g, b]
  }
  const lum = ([r, g, b]) => {
    const f = (c) => { const s = c / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4 }
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
  }
  const ratio = (fg, bg) => {
    const [x, y] = [lum(fg), lum(bg)]
    const [hi, lo] = x > y ? [x, y] : [y, x]
    return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100
  }
  /** Nearest opaque background behind `el`, walking up like the compositor. */
  const backdrop = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const c = paint(getComputedStyle(n).backgroundColor)
      if (c) return c
    }
    return paint(getComputedStyle(document.body).backgroundColor) || [255, 255, 255]
  }
  const cs = getComputedStyle(document.documentElement)
  const measured = {}
  const add = (name, el) => {
    if (!el) return
    const fg = paint(getComputedStyle(el).color)
    if (!fg) return
    const bg = backdrop(el)
    measured[name] = {
      color: `rgb(${fg.join(', ')})`,
      on: `rgb(${bg.join(', ')})`,
      ratio: ratio(fg, bg),
      text: (el.textContent || '').trim().slice(0, 32),
    }
  }

  // The densest run of secondary text in the product: a settings row's label and
  // its description, plus a segmented-control segment.
  const row = document.querySelector('[data-setting-label]')
  add('settings row label', row?.querySelector('div,label,span') || null)
  const desc = row?.querySelectorAll('div,span') || []
  add('settings row description', desc[desc.length - 1] || null)
  add('segmented segment', document.querySelector('[role="group"] button') || null)

  return {
    dataTheme: document.documentElement.dataset.theme,
    mode: document.documentElement.dataset.mode,
    tokens: {
      '--bg': cs.getPropertyValue('--bg').trim(),
      '--card': cs.getPropertyValue('--card').trim(),
      '--muted': cs.getPropertyValue('--muted').trim(),
      '--muted-fg': cs.getPropertyValue('--muted-fg').trim(),
      '--text-strong': cs.getPropertyValue('--text-strong').trim(),
      '--accent-fg': cs.getPropertyValue('--accent-fg').trim(),
    },
    measured,
  }
}

await page.goto(`${base}/settings?tab=display`, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-setting-label]', { timeout: 15_000 })
await freezeMotion(page)
await page.waitForTimeout(700)

const probe = await page.evaluate(PROBE)
if (probe.dataTheme !== `custom-${SLUG}-light`) {
  throw new Error(`pack not active — data-theme is ${probe.dataTheme}, expected custom-${SLUG}-light`)
}

await page.screenshot({ path: join(OUT, `${LABEL}-settings-display.png`), fullPage: false })
console.log(`captured ${LABEL}-settings-display.png`)

await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(900)
await page.screenshot({ path: join(OUT, `${LABEL}-session-list.png`), fullPage: false })
console.log(`captured ${LABEL}-session-list.png`)

writeFileSync(join(OUT, `${LABEL}-tokens.json`), JSON.stringify(probe, null, 2) + '\n')
console.log(`\n[${LABEL}] data-theme=${probe.dataTheme} mode=${probe.mode}`)
for (const [k, v] of Object.entries(probe.tokens)) console.log(`  ${k.padEnd(16)} ${v}`)
console.log('  measured on the rendered page:')
for (const [k, m] of Object.entries(probe.measured)) {
  console.log(`    ${k.padEnd(26)} ${String(m.ratio).padStart(6)}:1  ${m.ratio >= 4.5 ? 'AA' : 'FAIL'}  ${m.color} on ${m.on}`)
}

await browser.close()
srv.close()

if (LABEL === 'after') {
  const names = Object.keys(probe.measured)
  if (names.length < 2) {
    console.error(`\nFAIL — probe measured only ${names.length} element(s); the selectors have drifted.`)
    process.exit(1)
  }
  const failing = Object.entries(probe.measured).filter(([, m]) => m.ratio < 4.5)
  if (failing.length) {
    console.error(`\nFAIL — ${failing.length} rendered element(s) below WCAG AA after the fix:`)
    for (const [k, m] of failing) console.error(`  ${k} = ${m.ratio}:1 (${m.color} on ${m.on})`)
    process.exit(1)
  }
  console.log('\nOK — every measured element clears WCAG AA on the pack surface.')
}

