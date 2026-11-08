/**
 * Screenshot harness for #18153: a session whose CREATOR HAS CLOSED must read a
 * graceful "a closed session" label on both surfaces instead of the raw slot id
 * it carried before.
 *
 * Two visible surfaces, each over the committed isolated capture entry that mounts
 * the REAL component with the wire the slots/memory payload really returns for a
 * closed creator (`parent.key === null`, the creator absent from the live rows):
 *
 *   1. the chat SIDEBAR's closed-creator row  -> pages/chat-sidebar/SessionRow.tsx
 *      renders the orphan glyph whose label is "Opened by a closed session".
 *   2. the SYSTEM page's Sessions TABLE creator column -> pages/system/SessionsTab.tsx
 *      renders "Created by a closed session" on the row whose creator closed.
 *
 * Gateway-free: a vite dev server is started IN-PROCESS to serve the capture pages
 * (website/capture/*.html, which are build-excluded and only ever served by a dev
 * server), so there is no pod, no backend and no external :6181 to stand up. Each
 * surface is shot in light AND dark, because the UX lane reads the body's images
 * surface-by-surface and theme-by-theme and says "cannot evaluate" on any it cannot
 * see.
 *
 * ASSERT-THEN-SHOOT: the graceful label text is asserted present in the DOM (and the
 * stale slot id asserted absent) BEFORE each PNG is written, so a frame can never be
 * an honest-looking image of the wrong state.
 *
 * The BEFORE state (the raw slot id, "Opened by chat-2552 (closed)") is origin/main's
 * render of these same entries; this PR is the fix, so what is captured here is the
 * AFTER on both surfaces and both themes. The delta is carried by the label string the
 * real component emits for the identical closed-creator fixture.
 *
 * Usage: node scripts/capture-18153-closed-creator-label.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createServer } from 'vite'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

// The capture entries seed their rows straight into the redux store, but the real
// ChatSidebar / SessionsTab still fetch boot endpoints (/api/chat/folders, ...) on
// mount; left un-answered on the bare dev server they 404 and crash the shell to its
// "Could not load folders" error boundary. Answer every /api/** from the shared stub
// and let the dev server serve everything else.
const stub = (page, theme) => stubDashboardApi(page, {
  theme,
  folders: [],
  extra: async (path, route) => {
    if (path.startsWith('/api/')) return false
    await route.continue()
    return true
  },
})

const OUT = process.argv[2] || 'temp-screenshots/18153-closed-creator-label'
mkdirSync(OUT, { recursive: true })

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed = true
}

// Dev server, in-process, serving the build-excluded capture entries.
const vite = await createServer({
  configFile: new URL('../vite.config.ts', import.meta.url).pathname,
  server: { port: 0 },
  logLevel: 'error',
})
await vite.listen()
const addr = vite.httpServer.address()
const BASE = `http://127.0.0.1:${addr.port}`
console.log(`dev server on ${BASE}`)

const browser = await chromium.launch({ executablePath: chromiumExecutable() })

const GRACEFUL_SIDEBAR = /Opened by a closed session/i
const GRACEFUL_TABLE = /Created by a closed session/i
const STALE_SLOT = /chat-2552/i

// A fresh context per frame: localStorage (where the capture entry writes the theme
// preference) does not carry over, so each theme is rendered honestly rather than
// inheriting the previous page's palette.
const newPage = async (theme, width, height) => {
  const context = await browser.newContext({ deviceScaleFactor: 2, viewport: { width, height } })
  const page = await context.newPage()
  page.on('pageerror', e => { console.log(`FAIL pageerror — ${e.message}`); failed = true })
  await stub(page, theme)
  logPageProblems(page)
  return { context, page }
}

try {
  // ── Surface 1: the chat sidebar's closed-creator row ─────────────────────
  for (const theme of ['light', 'dark']) {
    const { context, page } = await newPage(theme, 620, 640)
    // Default frame (no &reparent): each worker cites a conductor that is not in the
    // payload, so the sidebar draws the orphan/closed-creator glyph.
    await page.goto(`${BASE}/capture/session-tree-closed-conductor.html?theme=${theme}`,
      { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-capture-ready]', { timeout: 30_000 })
    await page.waitForSelector('[data-slot-key="chat-2548"]', { timeout: 30_000 })
    const glyph = page.locator('[data-testid^="conductor-orphan-"]').first()
    await glyph.waitFor({ state: 'visible', timeout: 15_000 })
    const aria = await glyph.getAttribute('aria-label')
    const title = await glyph.getAttribute('title')
    check(`sidebar/${theme}: glyph aria-label is the graceful label`, GRACEFUL_SIDEBAR.test(aria ?? ''), `aria=${aria}`)
    check(`sidebar/${theme}: glyph title is the graceful label`, GRACEFUL_SIDEBAR.test(title ?? ''), `title=${title}`)
    check(`sidebar/${theme}: glyph label carries no raw slot id`, !STALE_SLOT.test(aria ?? ''), `aria=${aria}`)
    await glyph.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    const inner = page.locator('.sidebar-inner')
    await (await inner.count() ? inner : page.locator('body')).screenshot({ path: `${OUT}/sidebar-closed-creator-${theme}.png` })
    console.log(`wrote ${OUT}/sidebar-closed-creator-${theme}.png`)

    // The glyph names itself through a native `title`/`aria-label`, which a
    // headless browser never paints and a blind-read screenshot therefore cannot
    // show -- the reader sees a bare "↳". Surface the tooltip HONESTLY: read the
    // label straight off the rendered element's own `title` attribute (never a
    // string typed here) and paint it as a caption anchored to the glyph, the way
    // the OS tooltip would sit, so the frame shows the real text the component
    // emits for this row.
    const tipShown = await glyph.evaluate((el, label) => {
      const r = el.getBoundingClientRect()
      const tip = document.createElement('div')
      tip.textContent = label
      // Anchor the caption's LEFT to the viewport edge (not the glyph's own left,
      // which sits far right in the row and pushed a nowrap caption off-frame), and
      // sit it just under the glyph's row so it still reads as that row's tooltip
      // while staying fully inside the captured frame.
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
      return el.getAttribute('title') // echo back what was actually painted
    }, title)
    check(`sidebar/${theme}: tooltip caption reflects the glyph's real title`, GRACEFUL_SIDEBAR.test(tipShown ?? ''), `title=${tipShown}`)
    await page.waitForTimeout(200)
    await (await inner.count() ? inner : page.locator('body')).screenshot({ path: `${OUT}/sidebar-closed-creator-tooltip-${theme}.png` })
    console.log(`wrote ${OUT}/sidebar-closed-creator-tooltip-${theme}.png`)
    await context.close()
  }

  // ── Surface 2: the System page's Sessions table creator column ───────────
  for (const theme of ['light', 'dark']) {
    const { context, page } = await newPage(theme, 1180, 620)
    // Default frame (no &reparent): each worker carries a null creator key, so the
    // table shows top-level rows whose creator closed -> the graceful label.
    await page.goto(`${BASE}/capture/sessions-table-closed-conductor.html?theme=${theme}`,
      { waitUntil: 'domcontentloaded' })
    await page.waitForSelector('[data-capture-ready]', { timeout: 30_000 })
    await page.waitForSelector('text=I1: MicroVM boot path and the lease', { timeout: 30_000 })
    const label = page.getByText(GRACEFUL_TABLE).first()
    await label.waitFor({ state: 'visible', timeout: 15_000 })
    const txt = await label.textContent()
    check(`table/${theme}: creator column shows the graceful label`, GRACEFUL_TABLE.test(txt ?? ''), `text=${txt}`)
    // No row's creator line may still read the raw slot id.
    const anySlotId = await page.getByText(STALE_SLOT).count()
    check(`table/${theme}: no creator line carries the raw slot id`, anySlotId === 0, `matches=${anySlotId}`)
    await label.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/sessions-table-closed-creator-${theme}.png` })
    console.log(`wrote ${OUT}/sessions-table-closed-creator-${theme}.png`)
    await context.close()
  }
} finally {
  await browser.close()
  await vite.close()
}

console.log(failed ? 'RESULT: FAIL' : 'RESULT: ok')
process.exit(failed ? 1 : 0)
