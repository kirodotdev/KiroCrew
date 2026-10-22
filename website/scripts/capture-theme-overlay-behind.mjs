/**
 * Screenshot + visibility probe for `layer: "behind"` theme overlays.
 *
 * A behind overlay paints at z-index -1 inside the dashboard shell: over the
 * shell's background, under the nav, content and panels. Whether it is visible
 * at all depends on every page leaving its content area transparent, which a
 * DOM test cannot show. So each frame is paired with a pixel check: the page is
 * shot with the overlay shown and again with it hidden, and the share of
 * changed pixels in the content column is reported. 0% means a page covers the
 * overlay completely.
 *
 * The pack is a fixture served through the API stub: one fullscreen overlay
 * drawing a lavender corner web, declared `layer: "behind"`. `useTheme` loads
 * it through the real `/api/themes` path and `ThemeExperienceLayer` mounts the
 * real overlay iframe into the real behind slot.
 *
 * Frames (1440x900 unless noted, 2x):
 *   chat-dark.png, chat-light.png   a session with messages
 *   sessions/schedule/artifacts/settings/apps.png
 *   chat-focus.png                  focus mode
 *   chat-mobile.png                 390x844
 *
 * Exit status is non-zero when any page hides the overlay completely.
 *
 * Usage: node scripts/capture-theme-overlay-behind.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/theme-overlay-behind'
mkdirSync(OUT, { recursive: true })

const SLUG = 'web-behind'
const OVERLAY_ID = 'web'
const SLOT = 'chat-web'

/** A corner web: rings and spokes from the top-right, lavender, high enough contrast to see under text. */
const OVERLAY_HTML = `<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:transparent;overflow:hidden}
  svg{position:fixed;top:0;right:0;width:min(900px,80vw);height:min(900px,100vh)}
  path,line{stroke:#c6a0ff;stroke-width:2;fill:none;opacity:.8}
</style></head><body><svg viewBox="0 0 900 900">
  ${Array.from({ length: 9 }, (_, i) => {
    const a = (Math.PI / 2) * (i / 8)
    return `<line x1="900" y1="0" x2="${900 - Math.cos(a) * 1300}" y2="${Math.sin(a) * 1300}"/>`
  }).join('')}
  ${Array.from({ length: 8 }, (_, i) => {
    const r = 110 * (i + 1)
    return `<path d="M${900 - r} 0 A${r} ${r} 0 0 0 900 ${r}"/>`
  }).join('')}
</svg></body></html>`

const THEME_ROW = { slug: SLUG, name: 'Web Behind', emoji: '🕸️', source: 'installed' }
const THEME_DETAIL = {
  name: 'Web Behind', slug: SLUG, emoji: '🕸️', level: 2, dark: {},
  // A pack with no light palette stays dark in light mode, so give the light frame real colors.
  light: {
    '--bg': '#f5f1f6', '--bg-accent': '#ece6ef', '--bg-elevated': '#ffffff', '--bg-hover': '#e4dce8',
    '--card': 'rgba(255,255,255,0.9)', '--panel': 'rgba(245,241,246,0.88)', '--chrome': 'rgba(245,241,246,0.94)',
    '--text': '#4a4054', '--text-strong': '#241b2d', '--muted': '#8a7e91', '--border': '#d8cfdd',
  },
  assets: {
    overlays: [{
      id: OVERLAY_ID, position: 'fullscreen', zIndex: 1, layer: 'behind',
      pointerEvents: false, animation: 'continuous', trigger: 'continuous',
    }],
  },
}

const now = Date.now() / 1000
const slots = [{
  key: SLOT, title: 'Why is the build failing on main?', running: false,
  last_message: 'Opened a pull request with the fix.', messages: 4,
  agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
  modified: Math.floor(now), source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: '',
  messages: [
    { role: 'user', ts: now - 900, content: 'Can you check why the build is failing on main? It started after the theme loader change this morning.' },
    { role: 'assistant', ts: now - 800, content: 'The build fails in `src/utils/date.ts`. The test expects UTC, but the runner uses local time, so the date at midnight comes out one day early. I pinned the time zone in the test setup and reran the suite.\n\n**Result:** 42 tests passed, 0 failed.' },
    { role: 'user', ts: now - 700, content: 'Great. Open a pull request and add a note about the time zone to the description.' },
    { role: 'assistant', ts: now - 600, content: 'Opened the pull request with the fix and a note that the test now runs in UTC no matter where the runner is.' },
  ],
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const results = {}

async function open(mode, viewport) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2 })
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots, theme: mode,
    localStorageEntries: { 'mc-color-theme': `custom-${SLUG}` },
    extra: async (path, route) => {
      const ok = (body, type = 'application/json') =>
        route.fulfill({ status: 200, contentType: type, body: typeof body === 'string' ? body : JSON.stringify(body) })
      if (path === '/api/themes') { await ok({ themes: [THEME_ROW], installed: [SLUG] }); return true }
      if (path === `/api/themes/${SLUG}`) { await ok(THEME_DETAIL); return true }
      if (path === '/api/theme/boot') { await ok({ mode, color: `custom-${SLUG}` }); return true }
      if (path === `/api/theme/${SLUG}/overlay/${OVERLAY_ID}`) { await ok(OVERLAY_HTML, 'text/html'); return true }
      if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
      return false
    },
  })
  return page
}

/** Shoot, then shoot again with the overlay hidden, and count changed pixels in the content column. */
async function shoot(page, name) {
  const frame = page.locator(`iframe[data-theme-layer="behind"]`)
  await frame.waitFor({ state: 'attached', timeout: 15_000 })
  await page.waitForTimeout(700)
  const box = await page.evaluate(() => {
    const el = document.querySelector('main') || document.querySelector('[data-testid="dashboard-shell"]')
    const r = el.getBoundingClientRect()
    return { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) }
  })
  const withArt = await page.screenshot({ path: `${OUT}/${name}.png` })
  await page.evaluate(() => { document.querySelector('iframe[data-theme-layer="behind"]').style.visibility = 'hidden' })
  await page.waitForTimeout(150)
  const without = await page.screenshot()
  await page.evaluate(() => { document.querySelector('iframe[data-theme-layer="behind"]').style.visibility = '' })
  const changed = await page.evaluate(async ({ a, b, box }) => {
    const load = src => new Promise(res => { const i = new Image(); i.onload = () => res(i); i.src = src })
    const [ia, ib] = await Promise.all([load(a), load(b)])
    const s = ia.width / innerWidth
    const read = img => { const c = document.createElement('canvas'); c.width = box.width * s; c.height = box.height * s; const x = c.getContext('2d'); x.drawImage(img, -box.x * s, -box.y * s); return x.getImageData(0, 0, c.width, c.height).data }
    const da = read(ia), db = read(ib)
    let n = 0
    for (let i = 0; i < da.length; i += 4) if (Math.abs(da[i] - db[i]) + Math.abs(da[i + 1] - db[i + 1]) + Math.abs(da[i + 2] - db[i + 2]) > 24) n++
    return n / (da.length / 4)
  }, { a: `data:image/png;base64,${withArt.toString('base64')}`, b: `data:image/png;base64,${without.toString('base64')}`, box })
  const parent = await frame.evaluate(f => f.parentElement?.id)
  results[name] = { visiblePct: +(changed * 100).toFixed(2), slot: parent }
  console.log(`${name}: overlay changes ${results[name].visiblePct}% of the content area (slot ${parent})`)
}

const desktop = { width: 1440, height: 900 }
for (const mode of ['dark', 'light']) {
  const page = await open(mode, desktop)
  await page.goto(`${base}/chat/${SLOT}`)
  await page.getByText('42 tests passed').waitFor({ timeout: 15_000 })
  await shoot(page, `chat-${mode}`)
  await page.close()
}

const pages = await open('dark', desktop)
for (const [name, path] of [['sessions', '/sessions'], ['schedule', '/schedule'], ['artifacts', '/artifacts'], ['settings', '/settings'], ['apps', '/apps']]) {
  await pages.goto(`${base}${path}`)
  await pages.waitForSelector('header.topbar')
  await shoot(pages, name)
}
await pages.close()

const focus = await open('dark', desktop)
await focus.goto(`${base}/chat/${SLOT}`)
await focus.getByText('42 tests passed').waitFor({ timeout: 15_000 })
await focus.getByRole('button', { name: /focus mode/i }).first().click()
await focus.waitForTimeout(400)
await shoot(focus, 'chat-focus')
await focus.close()

const mobile = await open('dark', { width: 390, height: 844 })
await mobile.goto(`${base}/chat/${SLOT}`)
await mobile.getByText('42 tests passed').waitFor({ timeout: 15_000 })
await shoot(mobile, 'chat-mobile')
await mobile.close()

await browser.close()
srv.close()

const hidden = Object.entries(results).filter(([, r]) => r.visiblePct === 0).map(([k]) => k)
if (hidden.length) {
  console.log(`FAIL: the behind overlay is fully covered on: ${hidden.join(', ')}`)
  process.exit(1)
}
console.log('PASS: the behind overlay shows on every captured page')
