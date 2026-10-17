/**
 * Screenshot + behavior check for "Show Full Titles While Scrolling"
 * (chat config `fullTitlesOnScroll`): while the sidebar session list is
 * scrolled, clipped titles show in full in a click-through chip layer.
 *
 * Runs the REAL built SPA (website/dist) behind lib/serve-dist.mjs with every
 * /api/** call answered by lib/stub-dashboard-api.mjs (gateway-free). A Vite
 * dev server does not work here: the stub's `**\/api/**` route also catches
 * dev-mode module URLs such as /src/api/*.ts. It ASSERTS as well as
 * photographs, so a frame that does not show what its name says exits non-zero:
 *   - before: no layer at rest, the pointed-at row is :hover;
 *   - scrolling: chips render, the :hover row has none, and no chip's right
 *     edge crosses the composer dock's left edge;
 *   - after-leave: the layer is gone once the pointer leaves the sidebar.
 *
 * Stills use reduced motion so chips are fully opaque (the layer fades over
 * 120 ms otherwise); the optional demo video runs with motion on.
 *
 * Usage (after `npm run build`, or `vite build`, in website/):
 *   node scripts/capture-sidebar-scroll-peek.mjs [outDir] [--video]
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const OUT = positional[0] || '../temp-screenshots/sidebar-scroll-peek'
const VIDEO = process.argv.includes('--video')
const { srv, base: BASE } = await serveDist()
const VIEWPORT = { width: 1280, height: 800 }

mkdirSync(OUT, { recursive: true })

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` - ${detail}` : ''}`)
  if (!ok) failed = true
}

// Long enough that a 220px sidebar clips every one of them.
const TITLES = [
  'Investigate the flaky Windows shard in the cron scheduler suite',
  'Rebase the notification bridge RFC onto the new event bus',
  'Audit every i18n locale for missing settings panel keys',
  'Subagent resumability across gateway restarts and upgrades',
  'Folder glyph collapse animation on narrow sidebar widths',
  'ShipIt swap race when two updates land within one minute',
  'Model switch in place without dropping the running turn',
  'Diagnostics collector redacts tokens before bundling logs',
  'Release runbook for the stable and preview update lanes',
  'Channel dispatch pipeline retries with exponential backoff',
  'Workspace mount permissions on shared developer desktops',
  'Memory governor caps sub-agent fan-out under host pressure',
  'Notarize manifest and staple the ticket to the app bundle',
  'Navigation animation polish for the settings sub-pages',
]

const slots = TITLES.map((title, i) => ({
  key: `s${i + 1}`,
  title,
  messages: 4,
  running: false,
  agent: 'kirocrew',
  created: '2026-10-01T01:00:00Z',
  last_ts: new Date(Date.parse('2026-10-13T16:00:00Z') - i * 3600_000).toISOString(),
  folder_id: '',
}))

const STORAGE = {
  'mc-chat-config': JSON.stringify({ fullTitlesOnScroll: true }),
  'mc-sidebar-width': '220',
  'mc-sidebar-pinned': 'true',
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })

async function openPage({ reducedMotion = 'reduce', recordVideo } = {}) {
  const context = await browser.newContext({
    viewport: VIEWPORT, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion,
    ...(recordVideo ? { recordVideo: { dir: recordVideo, size: VIEWPORT } } : {}),
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { slots, theme: 'dark', localStorageEntries: STORAGE })
  logPageProblems(page)
  return { context, page }
}

/** The scrollable ancestor of the first session row, tagged for later lookup. */
const tagLane = page => page.evaluate(() => {
  const row = document.querySelector('[data-slot-key="s1"]')
  let lane = row?.parentElement
  while (lane) {
    const oy = getComputedStyle(lane).overflowY
    if ((oy === 'auto' || oy === 'scroll') && lane.scrollHeight > lane.clientHeight) break
    lane = lane.parentElement
  }
  if (!lane) return null
  lane.setAttribute('data-peek-probe-lane', '1')
  const r = lane.getBoundingClientRect()
  return { x: r.x, y: r.y, width: r.width, height: r.height, scrollHeight: lane.scrollHeight }
})

/** Layer state: chip rects, hovered row key, composer dock left edge. */
const peekState = page => page.evaluate(() => {
  const layer = document.querySelector('[data-testid="scroll-peek-layer"]')
  const chips = Array.from(document.querySelectorAll('[data-scroll-peek-title]')).map(c => {
    const r = c.getBoundingClientRect()
    return { title: c.getAttribute('data-scroll-peek-title') || c.textContent, top: r.top, bottom: r.bottom, right: r.right, opacity: getComputedStyle(c).opacity }
  })
  const hovered = Array.from(document.querySelectorAll('[data-slot-key]')).find(el => el.matches(':hover'))
  const hr = hovered?.getBoundingClientRect()
  const dock = document.querySelector('[data-scroll-peek-stop]')?.getBoundingClientRect()
  return {
    layer: !!layer,
    layerOpacity: layer ? getComputedStyle(layer).opacity : null,
    chips,
    hoveredKey: hovered?.getAttribute('data-slot-key') || null,
    hoveredTitle: hovered?.textContent?.trim().slice(0, 60) || null,
    hoveredBox: hr ? { top: hr.top, bottom: hr.bottom } : null,
    dock: dock ? { left: dock.left, top: dock.top, bottom: dock.bottom } : null,
  }
})

async function boot(page) {
  await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-slot-key="s1"]', { timeout: 30000 })
  await page.waitForTimeout(1500)
  const lane = await tagLane(page)
  check('lane overflows so it can scroll', !!lane && lane.scrollHeight > lane.height, JSON.stringify(lane))
  return lane
}

// ── Stills
{
  const { context, page } = await openPage()
  const lane = await boot(page)
  // Rest the pointer on a row in the upper middle of the list.
  const px = lane.x + lane.width * 0.45
  const py = lane.y + Math.min(lane.height * 0.4, 260)
  await page.mouse.move(px, py)
  await page.waitForTimeout(400)
  let s = await peekState(page)
  check('before: a row is hovered', !!s.hoveredKey, s.hoveredTitle)
  check('before: no chips at rest', s.chips.length === 0, `${s.chips.length} chips`)
  await page.screenshot({ path: `${OUT}/before.png`, scale: 'css' })

  // Mid-scroll: a few wheel ticks with the pointer still.
  for (let i = 0; i < 3; i++) {
    await page.mouse.wheel(0, 100)
    await page.waitForTimeout(80)
  }
  await page.waitForTimeout(300)
  s = await peekState(page)
  check('scrolling: layer shows chips', s.layer && s.chips.length > 0, `${s.chips.length} chips`)
  check('scrolling: a row is hovered', !!s.hoveredKey, s.hoveredTitle)
  const onHovered = s.hoveredBox
    ? s.chips.filter(c => c.top < s.hoveredBox.bottom - 2 && c.bottom > s.hoveredBox.top + 2)
    : []
  check('scrolling: hovered row has no chip', onHovered.length === 0, JSON.stringify(onHovered))
  check('scrolling: composer dock is marked as a stop', !!s.dock, JSON.stringify(s.dock))
  if (s.dock) {
    const beside = s.chips.filter(c => c.bottom > s.dock.top && c.top < s.dock.bottom)
    const over = beside.filter(c => c.right > s.dock.left + 0.5)
    check('scrolling: chips beside the dock stop at its left edge', over.length === 0,
      `${beside.length} beside, rights ${beside.map(c => Math.round(c.right)).join(',')} vs dock ${Math.round(s.dock.left)}`)
    console.log(`INFO ${beside.length} chip(s) beside the composer dock`)
  }
  await page.screenshot({ path: `${OUT}/scrolling.png`, scale: 'css' })

  // Leave the sidebar: move far into the chat pane.
  await page.mouse.move(VIEWPORT.width - 200, 200)
  await page.waitForTimeout(600)
  s = await peekState(page)
  check('after-leave: no chips', s.chips.length === 0, `${s.chips.length} chips`)
  await page.screenshot({ path: `${OUT}/after-leave.png`, scale: 'css' })
  await context.close()
}

// ── Settings toggle row
{
  const { context, page } = await openPage()
  await page.goto(`${BASE}/settings/chat/sessions`, { waitUntil: 'domcontentloaded' })
  const label = page.getByText('Show Full Titles While Scrolling', { exact: true })
  await label.first().waitFor({ timeout: 30000 })
  await page.waitForTimeout(800)
  // The description lives in the (i) tooltip beside the label, so hover it.
  const rowHandle = await label.first().evaluateHandle(el => {
    let row = el
    for (let i = 0; i < 6 && row.parentElement; i++) {
      row = row.parentElement
      if (row.querySelector('[role="switch"], input[type="checkbox"], button[aria-pressed], button[aria-checked]')) break
    }
    return row
  })
  const info = await rowHandle.evaluateHandle(row => row.querySelector('[data-settings-hint] > *'))
  const infoEl = info.asElement()
  if (infoEl) { await infoEl.hover(); await page.waitForTimeout(700) }
  const hint = page.getByText('While you scroll the session list, titles the sidebar cuts off show in full')
  check('settings: new description renders', (await hint.count()) > 0 && (await hint.first().isVisible()))
  const rb = await rowHandle.evaluate(r => { const b = r.getBoundingClientRect(); return { x: b.x, y: b.y, r: b.right, b: b.bottom } })
  const hb = (await hint.count()) ? await hint.first().boundingBox() : null
  const x0 = Math.max(0, Math.min(rb.x, hb ? hb.x : rb.x) - 16)
  const y0 = Math.max(0, Math.min(rb.y, hb ? hb.y : rb.y) - 40)
  const x1 = Math.min(VIEWPORT.width, Math.max(rb.r, hb ? hb.x + hb.width : rb.r) + 16)
  const y1 = Math.min(VIEWPORT.height, Math.max(rb.b, hb ? hb.y + hb.height : rb.b) + 40)
  const clip = { x: x0, y: y0, width: x1 - x0, height: y1 - y0 }
  await page.screenshot({ path: `${OUT}/settings-toggle.png`, scale: 'css', clip })
  await context.close()
}

// ── Optional demo video, motion on so the 120 ms fade is visible.
if (VIDEO) {
  const tmp = join(OUT, '.video-tmp')
  rmSync(tmp, { recursive: true, force: true })
  const { context, page } = await openPage({ reducedMotion: 'no-preference', recordVideo: tmp })
  const lane = await boot(page)
  const px = lane.x + lane.width * 0.45
  const py = lane.y + Math.min(lane.height * 0.4, 260)
  await page.mouse.move(px, py, { steps: 10 })
  await page.waitForTimeout(800)
  for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, 60); await page.waitForTimeout(150) }
  await page.waitForTimeout(1200)
  for (let i = 0; i < 4; i++) { await page.mouse.wheel(0, -60); await page.waitForTimeout(150) }
  await page.waitForTimeout(800)
  await page.mouse.move(VIEWPORT.width - 200, 200, { steps: 10 })
  await page.waitForTimeout(1200)
  const video = page.video()
  await context.close()
  const src = video ? await video.path() : join(tmp, readdirSync(tmp)[0])
  renameSync(src, join(OUT, 'demo.webm'))
  rmSync(tmp, { recursive: true, force: true })
  console.log('wrote', join(OUT, 'demo.webm'))
}

await browser.close()
srv.close()
process.exit(failed ? 1 : 0)
