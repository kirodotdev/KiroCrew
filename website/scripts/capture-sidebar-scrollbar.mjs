/**
 * Screenshot + measurement harness for the chat sidebar's vertical scrollbar.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend). Seeds enough root sessions to
 * guarantee the session lane overflows, because the whole point of the change is
 * only visible while the lane is scrollable.
 *
 * Screenshots alone are weak evidence here: the change REMOVES a 6px stripe, and
 * "I don't see it" is not proof. Worse, they are NO evidence at all in headless
 * — measured 2026-07-30, the before/after PNGs come out BYTE-IDENTICAL because
 * headless Chromium uses macOS overlay scrollbars, which take no layout space
 * and are not painted at rest. `--disable-features=OverlayScrollbar` does not
 * change that. A real Electron/browser window does render the custom 6px bar
 * from ::-webkit-scrollbar in index.css (see the 6px inset comment in
 * ChatPage.tsx), so this harness under-reports the real UI.
 *
 * What it CAN prove, and what the probe below reports:
 *   - the computed `scrollbar-width` on the exact lane element (auto -> none),
 *   - that the lane is still genuinely scrollable (scrollHeight > clientHeight),
 *     so a "hidden" result that came from killing the overflow cannot pass,
 *   - that a driven scroll still moves it (scrollTop honoured).
 * Treat the PNG as a layout confidence check only, never as the scrollbar verdict.
 *
 * Usage: node scripts/capture-sidebar-scrollbar.mjs [outDir] [prefix]
 *   Run against the branch (after) and against a main build (before).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sidebar-scrollbar'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

// No folders: exercise the default single-lane layout, which is the surface the
// report was about ("the chat sessions side bar").
const folders = []

const TITLES = [
  'Sidebar scrollbar', 'Auto-update install flow', 'Tips Kit T1 analyzer',
  'StyledSelect retirement', 'App Store revamp', 'Linux CDN links',
  'Notification bridge RFC', 'Weixin QR render fix', 'i18n missing keys',
  'Subagent resumability', 'Folder glyph collapse', 'ShipIt swap race',
  'Model switch in place', 'Diagnostics collector', 'Release runbook SOP',
  'Channel dispatch pipeline', 'Workspace mount', 'Memory governor',
  'Notarize manifest', 'Nav animation polish', 'PEP 503 index', 'CLI installer',
  'Cost formatting', 'Version bump lane', 'UX review lane', 'Frontend skill',
]

// 26 root sessions at ~34px a row overflows the lane at any sane window height.
const slots = TITLES.map((title, i) => ({
  key: `s${i + 1}`,
  title,
  messages: 4,
  running: false,
  agent: 'kirocrew',
  created: '2026-07-20T01:00:00Z',
  last_ts: new Date(Date.parse('2026-07-29T21:00:00Z') - i * 3600_000).toISOString(),
  folder_id: '',
}))

async function main() {
  const { srv, base } = await serveDist()
  // Kept as a best-effort attempt to get classic, always-visible scrollbars in
  // headless. Measured 2026-07-30: it does NOT work — the gutter stays 0 and the
  // frames stay byte-identical. Left in (harmless) with this note so the next
  // person does not spend the same hour rediscovering it; use the computed
  // scrollbar-width from the probe as the verdict instead of the pixels.
  const browser = await chromium.launch({ args: ['--disable-features=OverlayScrollbar'] })
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px sidebar type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  // Boot fixtures live in lib/stub-dashboard-api.mjs — shared by every
  // harness so a new boot endpoint is one edit, not one per script.
  await stubDashboardApi(page, { folders, slots })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // The session lane is the nearest ancestor of a rendered session row whose
  // COMPUTED overflow-y actually scrolls. Selecting on computed overflow rather
  // than on a class keeps the probe independent of the classes under test — and
  // rather than on "scrollHeight > clientHeight", which also matches an
  // overflow-hidden wrapper that reports overflow but cannot scroll.
  const probe = await page.evaluate(async () => {
    const row = document.querySelector('[data-slot-key="s1"]')
    if (!row) return { error: 'no session rows rendered' }
    let lane = row.parentElement
    while (lane) {
      const oy = getComputedStyle(lane).overflowY
      if ((oy === 'auto' || oy === 'scroll') && lane.scrollHeight > lane.clientHeight) break
      lane = lane.parentElement
    }
    if (!lane) return { error: 'no scrollable ancestor found' }

    const result = {
      gutterPx: lane.offsetWidth - lane.clientWidth,
      scrollable: lane.scrollHeight > lane.clientHeight,
      scrollHeight: lane.scrollHeight,
      clientHeight: lane.clientHeight,
      computedScrollbarWidth: getComputedStyle(lane).scrollbarWidth,
      laneClass: lane.className,
    }

    // Scrolling must still work — hiding the bar is cosmetic only.
    lane.scrollTop = 200
    await new Promise(r => requestAnimationFrame(r))
    result.scrollTopAfterScroll = lane.scrollTop
    lane.scrollTop = 0

    lane.setAttribute('data-scrollbar-probe', '1')
    return result
  })
  console.log(`PROBE ${PREFIX}`, JSON.stringify(probe))

  const lane = page.locator('[data-scrollbar-probe="1"]')
  const box = (await lane.count()) ? await lane.first().boundingBox() : null
  // Frame the lane plus ~10px of slack on each side so the right edge — where
  // the scrollbar track would sit — is unambiguously in shot.
  const clip = box
    ? { x: Math.max(0, box.x - 10), y: box.y, width: box.width + 20, height: Math.min(box.height, 760) }
    : { x: 470, y: 118, width: 380, height: 760 }
  await page.screenshot({ path: `${OUT}/${PREFIX}-01-session-lane.png`, clip })
  console.log('wrote', `${OUT}/${PREFIX}-01-session-lane.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
