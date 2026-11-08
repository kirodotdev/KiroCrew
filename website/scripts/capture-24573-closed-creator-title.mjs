/**
 * Screenshot harness for #24573: a session whose CREATOR HAS CLOSED is named by that
 * creator's LAST-KNOWN title when the backend still remembers it (`parent.title`), on
 * both surfaces, instead of the generic "a closed session" label #18153 shipped.
 *
 * Same committed capture entries and harness shape as
 * capture-18153-closed-creator-label.mjs (gateway-free, in-process vite dev server,
 * light AND dark, assert-then-shoot), with `&title=` so the fixture carries the
 * `parent.title` the lineage join now sends. The BEFORE is that script's output: the
 * identical fixture without a title -- which is also what a creator this gateway never
 * saw live still renders.
 *
 *   1. the chat SIDEBAR's closed-creator glyph (top-level orphan rows).
 *   2. the SYSTEM page's Sessions TABLE creator line (rows re-parented onto the open
 *      lead, `&reparent=1`).
 *
 * Usage: node scripts/capture-24573-closed-creator-title.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const stub = (page, theme) => stubDashboardApi(page, {
  theme,
  folders: [],
  extra: async (path, route) => {
    if (path.startsWith('/api/')) return false
    await route.continue()
    return true
  },
})

const OUT = process.argv[2] || 'temp-screenshots/24573-closed-creator-title'
mkdirSync(OUT, { recursive: true })

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed = true
}

const vite = await createServer({
  configFile: new URL('../vite.config.ts', import.meta.url).pathname,
  server: { port: 0 },
  logLevel: 'error',
})
await vite.listen()
const BASE = vite.resolvedUrls.local[0].replace(/\/$/, '')
console.log(`dev server on ${BASE}`)

const browser = await chromium.launch({ executablePath: chromiumExecutable() })

const TITLE = 'Remote crew conductor'
const NAMED_SIDEBAR = /^Opened by Remote crew conductor \(closed\)$/
const NAMED_TABLE = /^Created by Remote crew conductor \(closed\)$/
const STALE_SLOT = /chat-2552/i
const q = `&title=${encodeURIComponent(TITLE)}`

const newPage = async (theme, width, height) => {
  const context = await browser.newContext({ deviceScaleFactor: 2, viewport: { width, height } })
  const page = await context.newPage()
  page.on('pageerror', e => { console.log(`FAIL pageerror — ${e.message}`); failed = true })
  await stub(page, theme)
  logPageProblems(page)
  return { context, page }
}

try {
  // ── Surface 1: the chat sidebar's closed-creator glyph ───────────────────
  for (const theme of ['light', 'dark']) {
    const { context, page } = await newPage(theme, 620, 640)
    await page.goto(`${BASE}/capture/session-tree-closed-conductor.html?theme=${theme}${q}`,
      { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-capture-ready]', { timeout: 30_000 })
    await page.waitForSelector('[data-slot-key="chat-2548"]', { timeout: 30_000 })
    const glyph = page.locator('[data-testid^="conductor-orphan-"]').first()
    await glyph.waitFor({ state: 'visible', timeout: 15_000 })
    const aria = await glyph.getAttribute('aria-label')
    const title = await glyph.getAttribute('title')
    check(`sidebar/${theme}: glyph aria-label names the closed creator`, NAMED_SIDEBAR.test(aria ?? ''), `aria=${aria}`)
    check(`sidebar/${theme}: glyph title names the closed creator`, NAMED_SIDEBAR.test(title ?? ''), `title=${title}`)
    check(`sidebar/${theme}: glyph label carries no raw slot id`, !STALE_SLOT.test(aria ?? ''), `aria=${aria}`)
    await glyph.scrollIntoViewIfNeeded()
    // A headless browser never paints a native tooltip, so paint the glyph's OWN
    // title attribute as a caption under its row (read off the element, never typed).
    const tipShown = await glyph.evaluate(el => {
      const r = el.getBoundingClientRect()
      const tip = document.createElement('div')
      tip.textContent = el.getAttribute('title')
      Object.assign(tip.style, {
        position: 'fixed', left: '12px', top: `${Math.round(r.bottom + 6)}px`,
        zIndex: '2147483647', padding: '4px 8px', borderRadius: '6px',
        font: '12px/1.3 ui-sans-serif, system-ui, sans-serif', whiteSpace: 'nowrap',
        color: getComputedStyle(document.documentElement).getPropertyValue('--color-bg') || '#fff',
        background: getComputedStyle(document.documentElement).getPropertyValue('--color-fg') || '#111',
        boxShadow: '0 2px 10px rgba(0,0,0,0.35)', pointerEvents: 'none',
      })
      tip.setAttribute('data-capture-tooltip', '')
      document.body.appendChild(tip)
      return tip.textContent
    })
    check(`sidebar/${theme}: tooltip caption is the glyph's real title`, NAMED_SIDEBAR.test(tipShown ?? ''), `title=${tipShown}`)
    await page.waitForTimeout(400)
    // The whole page, not `.sidebar-inner`: the caption is wider than the sidebar
    // column, and cropping to the column cut the label off mid-word.
    await page.screenshot({ path: `${OUT}/sidebar-closed-creator-title-${theme}.png` })
    console.log(`wrote ${OUT}/sidebar-closed-creator-title-${theme}.png`)
    await context.close()
  }

  // ── Surface 2: the Sessions table creator line (re-parented rows) ────────
  for (const theme of ['light', 'dark']) {
    const { context, page } = await newPage(theme, 1180, 620)
    await page.goto(`${BASE}/capture/sessions-table-closed-conductor.html?theme=${theme}&reparent=1${q}`,
      { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-capture-ready]', { timeout: 30_000 })
    await page.waitForSelector('text=I1: MicroVM boot path and the lease', { timeout: 30_000 })
    const label = page.getByText(NAMED_TABLE).first()
    await label.waitFor({ state: 'visible', timeout: 15_000 })
    const txt = await label.textContent()
    check(`table/${theme}: creator line names the closed creator`, NAMED_TABLE.test(txt ?? ''), `text=${txt}`)
    const generic = await page.getByText(/Created by a closed session/).count()
    check(`table/${theme}: no row falls back to the generic label`, generic === 0, `matches=${generic}`)
    const anySlotId = await page.getByText(STALE_SLOT).count()
    check(`table/${theme}: no creator line carries the raw slot id`, anySlotId === 0, `matches=${anySlotId}`)
    await label.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/sessions-table-closed-creator-title-${theme}.png` })
    console.log(`wrote ${OUT}/sessions-table-closed-creator-title-${theme}.png`)
    await context.close()
  }
} finally {
  await browser.close()
  await vite.close()
}

console.log(failed ? 'RESULT: FAIL' : 'RESULT: ok')
process.exit(failed ? 1 : 0)
