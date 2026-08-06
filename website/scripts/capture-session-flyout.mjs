/**
 * Screenshot + video harness for the collapsed-sidebar session flyout.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route
 * interception (gateway-free — no kiro-cli, no live backend).
 *
 * A still frame cannot prove this change. The feature IS a sequence — hover
 * intent (a 320ms rest that a pointer sweep must not trigger), the flyout
 * arriving, and the click that grows that same rect into the real sidebar. So
 * this harness records video of the whole sequence and also emits the discrete
 * frames worth reviewing side by side.
 *
 * It also probes the things pixels cannot show:
 *   - the flyout's rect shares the panel rect's origin and width, so expanding
 *     moves only the bottom edge (the corner the eye is fixated on is pinned),
 *   - the chat pane does NOT reflow when the flyout opens (the whole point of
 *     "the page must not make room for it"),
 *   - a pointer sweep across the trigger leaves no flyout behind.
 *
 * Usage: node scripts/capture-session-flyout.mjs [outDir] [prefix] [dark|light]
 *
 * Both modes are worth capturing: the surface is a floating panel over content,
 * so it leans entirely on `--bg-elevated` + `--border` + `--shadow-lg` for
 * separation, and those three have very different contrast budgets per mode.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-flyout'
const PREFIX = process.argv[3] || 'after'
const THEME = process.argv[4] || 'dark'

mkdirSync(OUT, { recursive: true })
mkdirSync(`${OUT}/video`, { recursive: true })

// No folders: the flyout is a flat recents list by definition, so the default
// single-lane layout is the surface under test.
const folders = []

// Titles long enough to exercise truncation, and one deliberately short, so the
// frame shows both. Recency is strictly decreasing in array order, and the
// fixture order is DELIBERATELY NOT the expected render order — s1 is seeded
// mid-pack so a harness that simply echoed the fixture would be visible.
// Generic on purpose: these strings land in PR screenshots, so they must not
// name anything from a private codebase.
const SEED = [
  { key: 's1', title: 'Dependency upgrade sweep across the monorepo', hoursAgo: 3 },
  { key: 's2', title: 'Streaming responses over the message transport', hoursAgo: 0.05, running: true },
  { key: 's3', title: 'File chips — fade the line highlight, accept ranges', hoursAgo: 1, pending_approval: true },
  { key: 's4', title: 'Localise the app manifest strings', hoursAgo: 6 },
  { key: 's5', title: 'Terminal nav active-state fix', hoursAgo: 26 },
  { key: 's6', title: 'Printable logo, third pass', hoursAgo: 30 },
  { key: 's7', title: 'Browser language detection picks the wrong script', hoursAgo: 34 },
  { key: 's8', title: 'Product-name CI gate', hoursAgo: 50 },
  { key: 's9', title: 'Scheduling RFC', hoursAgo: 74 },
  { key: 's10', title: 'Coverage combine flake', hoursAgo: 98 },
  { key: 's11', title: 'Render gate surface coverage', hoursAgo: 120 },
]
const NOW = Date.parse('2026-08-06T01:00:00Z')
const slots = SEED.map(s => ({
  key: s.key,
  title: s.title,
  messages: 6,
  running: !!s.running,
  pending_approval: !!s.pending_approval,
  agent: 'kirocrew',
  created: '2026-07-20T01:00:00Z',
  last_ts: new Date(NOW - s.hoursAgo * 3600_000).toISOString(),
  folder_id: '',
}))

const TRIGGER = 'button[aria-haspopup="menu"].pi-morph'
const FLYOUT = '[role="menu"][aria-label="Recent sessions"]'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    // 12-13px row type renders soft at 1x; the video stays 1x for size.
    deviceScaleFactor: 2,
    // Video only on the dark run: the motion is identical per mode, and a
    // second recording is pure weight.
    ...(THEME === 'dark' ? { recordVideo: { dir: `${OUT}/video`, size: { width: 1400, height: 900 } } } : {}),
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { folders, slots, theme: THEME })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Brand-mark check, because a broken one is easy to miss in a frame you are
  // reviewing for something else — and it WAS missed for a long time (see
  // lib/serve-dist.mjs). Fails the capture rather than shipping a wrong frame.
  const brand = await page.evaluate(() => {
    const img = document.querySelector('img[aria-hidden="true"]')
    const word = document.querySelector('.font-bold.uppercase')
    return {
      logoLoaded: !!img && img.complete && img.naturalWidth > 0,
      logoSrc: img?.getAttribute('src') ?? null,
      wordmark: word?.textContent?.trim() ?? null,
    }
  })
  if (!brand.logoLoaded || !/\s/.test(brand.wordmark ?? '')) {
    throw new Error(`brand mark did not render: ${JSON.stringify(brand)} — `
      + 'the logo is a server route (see lib/serve-dist.mjs) and the bot name '
      + 'must stay two words (see lib/stub-dashboard-api.mjs).')
  }
  console.log(`BRAND ${PREFIX}`, JSON.stringify(brand))

  // Collapse the sidebar — the flyout only exists in the collapsed state.
  // Driven through the real toggle, not localStorage, so the collapse morph
  // itself is on the recording.
  await page.locator('.pi-morph').first().click()
  await page.waitForTimeout(900)
  await page.mouse.move(900, 600) // park the pointer well clear
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${PREFIX}-01-collapsed.png` })

  // ── Probe 1: a pointer SWEEP must leave nothing behind ──────────────────
  const btn = await page.locator('.pi-morph').first().boundingBox()
  await page.mouse.move(btn.x + btn.width / 2, btn.y + btn.height / 2)
  await page.waitForTimeout(120)            // well under the 320ms intent delay
  await page.mouse.move(900, 600)
  await page.waitForTimeout(700)
  const sweptOpen = await page.locator(FLYOUT).count()

  // ── Probe 2: hover and rest — the flyout arrives ────────────────────────
  // Measure the chat pane BEFORE, so "the page does not make room for it" is
  // asserted rather than eyeballed.
  const paneBefore = await page.evaluate(() => {
    const composer = document.querySelector('textarea[aria-label="Message input"]')
    const r = composer?.getBoundingClientRect()
    return r ? { x: Math.round(r.x), w: Math.round(r.width) } : null
  })

  // Sample the entrance every frame. A still cannot tell "grew out of the
  // button" from "faded in at full size" — both animate. The clip ladder can:
  // the window must START at the button's rect and OPEN monotonically.
  await page.evaluate((sel) => {
    window.__clips = []
    const t0 = performance.now()
    const tick = () => {
      const el = document.querySelector(sel)
      if (el) {
        const cs = getComputedStyle(el)
        window.__clips.push({ t: Math.round(performance.now() - t0), clip: cs.clipPath })
      }
      if (performance.now() - t0 < 900) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  }, FLYOUT)

  await page.mouse.move(btn.x + btn.width / 2, btn.y + btn.height / 2)
  await page.waitForSelector(FLYOUT, { timeout: 3000 })
  await page.waitForTimeout(450)
  await page.screenshot({ path: `${OUT}/${PREFIX}-02-flyout-open.png` })

  // Bottom inset over time: starts large (window is button-sized) and falls to 0
  // (window is the whole surface), never rising.
  const clipLadder = await page.evaluate(() => {
    const bottomOf = (c) => {
      const m = /inset\(([^)]*)\)/.exec(c || '')
      if (!m) return null
      const parts = m[1].split('round')[0].trim().split(/\s+/).map(v => parseFloat(v))
      return parts.length >= 3 ? parts[2] : null
    }
    const seen = []
    for (const s of window.__clips) {
      const b = bottomOf(s.clip)
      if (b === null) { seen.push({ t: s.t, bottom: 'none' }); continue }
      if (!seen.length || seen[seen.length - 1].bottom !== b) seen.push({ t: s.t, bottom: b })
    }
    return seen
  })
  const bottoms = clipLadder.filter(x => typeof x.bottom === 'number').map(x => x.bottom)
  // Assert the SHAPE, not a fraction of the travel. How far into the animation
  // the FIRST frame lands depends on machine load — the height is measured in a
  // layout effect and framer begins a keyframe array on the following frame, so
  // a busy runner can paint its first sample well past halfway. Measured 147px
  // under load vs 263px idle, which is why an earlier `> half the travel` check
  // was flaky rather than wrong-in-substance.
  //
  // Shape alone already separates the two cases completely: a fade-in at full
  // size emits `clip-path: none` for EVERY sample (no numeric entries at all),
  // while a grow emits a monotonically closing inset ladder. The absolute floor
  // just rules out "already essentially open on the first frame".
  const clipGrows = bottoms.length >= 6
    && bottoms[0] > 50
    && bottoms[bottoms.length - 1] <= 1
    && bottoms.every((b, i) => i === 0 || b <= bottoms[i - 1])
  if (!clipGrows) {
    throw new Error('flyout did not GROW out of the button — clip ladder: '
      + JSON.stringify(clipLadder.slice(0, 14)))
  }
  console.log(`CLIP ${PREFIX} steps=${bottoms.length} `
    + `bottom ${bottoms[0]} -> ${bottoms[bottoms.length - 1]} (monotonic, never rises)`)

  const paneAfter = await page.evaluate(() => {
    const composer = document.querySelector('textarea[aria-label="Message input"]')
    const r = composer?.getBoundingClientRect()
    return r ? { x: Math.round(r.x), w: Math.round(r.width) } : null
  })

  // Tight crop of just the flyout, for review at a readable size.
  const flyBox = await page.locator(FLYOUT).boundingBox()
  await page.screenshot({
    path: `${OUT}/${PREFIX}-03-flyout-crop.png`,
    clip: { x: flyBox.x - 14, y: flyBox.y - 14, width: flyBox.width + 28, height: flyBox.height + 28 },
  })

  // Row order + geometry, read off the live DOM.
  const probe = await page.evaluate(({ flySel }) => {
    const fly = document.querySelector(flySel)
    const rows = Array.from(fly.querySelectorAll('[data-slot-key]'))
    const container = fly.parentElement
    const c = container.getBoundingClientRect()
    const f = fly.getBoundingClientRect()
    return {
      rowOrder: rows.map(r => r.getAttribute('data-slot-key')),
      rowTitles: rows.map(r => r.textContent.trim().slice(0, 44)),
      menuItems: fly.querySelectorAll('[role="menuitem"]').length,
      tabStops: Array.from(fly.querySelectorAll('[role="menuitem"]')).filter(e => e.tabIndex >= 0).length,
      showAll: !!Array.from(fly.querySelectorAll('[role="menuitem"]'))
        .find(e => !e.hasAttribute('data-slot-key') && /show all/i.test(e.textContent)),
      flyRect: { x: Math.round(f.x - c.x), y: Math.round(f.y - c.y), w: Math.round(f.width), h: Math.round(f.height) },
      // The New button's rect, which must land on the toggle button's own
      // y-band (9..37) — that is what proves the header does not move on expand.
      newBtnY: (() => {
        const b = Array.from(fly.querySelectorAll('[role="menuitem"]'))
          .find(e => !e.hasAttribute('data-slot-key'))
        if (!b) return null
        const r = b.getBoundingClientRect()
        return { top: Math.round(r.top - c.top), bottom: Math.round(r.bottom - c.top) }
      })(),
    }
  }, { flySel: FLYOUT })

  // ── Probe 3: hover a row, then CLICK the toggle to grow it ──────────────
  await page.hover(`${FLYOUT} [data-slot-key="s4"]`)
  await page.waitForTimeout(350)
  await page.screenshot({ path: `${OUT}/${PREFIX}-04-row-hover.png` })

  // Mid-morph frames. The clip window animates over 240ms and a screenshot is
  // not instantaneous, so sample a ladder rather than one guessed instant —
  // a single 110ms sample landed AFTER the animation finished.
  await page.locator(TRIGGER).click()
  for (const [i, ms] of [40, 90, 150].entries()) {
    await page.waitForTimeout(i === 0 ? ms : ms - [40, 90, 150][i - 1])
    await page.screenshot({ path: `${OUT}/${PREFIX}-05-morph-${String(ms).padStart(3, '0')}ms.png` })
  }
  await page.waitForTimeout(900)
  await page.screenshot({ path: `${OUT}/${PREFIX}-06-expanded.png` })

  // ── Probe 4: keyboard parity — tabbing ONTO the trigger opens it too ────
  await page.locator('.pi-morph').first().click()   // collapse again
  await page.waitForTimeout(900)
  // Park focus AND the pointer elsewhere first. Clicking the toggle leaves it
  // focused, and `.focus()` on an already-focused element fires no `focusin`,
  // so probing without this measures nothing (it reported a false pass once).
  await page.locator('textarea[aria-label="Message input"]').focus()
  await page.mouse.move(900, 700)
  await page.waitForTimeout(500)
  const beforeFocus = await page.locator(FLYOUT).count()

  await page.locator(TRIGGER).focus()
  await page.waitForTimeout(400)
  // Focus alone must NOT open it: opening on focus and pulling focus in is a
  // WCAG 3.2.1 change of context, and it would make the trigger impossible to
  // Tab past.
  const openedOnFocusAlone = await page.locator(FLYOUT).count()

  // ArrowDown is the ARIA menu-button opener, and it lands focus on a row.
  await page.keyboard.press('ArrowDown')
  await page.waitForSelector(FLYOUT, { timeout: 3000 })
  await page.waitForTimeout(350)
  const arrowOpen = await page.locator(FLYOUT).count()
  const focusedRow = await page.evaluate(() =>
    document.activeElement?.getAttribute('data-slot-key') ?? null)
  await page.keyboard.press('ArrowDown')
  await page.waitForTimeout(200)
  const afterArrow = await page.evaluate(() =>
    document.activeElement?.getAttribute('data-slot-key') ?? null)
  await page.screenshot({ path: `${OUT}/${PREFIX}-07-keyboard-open.png` })

  // Escape must dismiss AND hand focus back to the trigger — otherwise
  // activeElement falls to <body> and the next Tab restarts from the top of the
  // page. This reads focus, rather than only asserting the surface is gone.
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)
  const afterEscape = await page.locator(FLYOUT).count()
  const focusReturnedToTrigger = await page.evaluate((sel) =>
    document.activeElement === document.querySelector(sel), TRIGGER)

  console.log(`PROBE ${PREFIX} ${JSON.stringify({
    sweepLeftNothing: sweptOpen === 0,
    paneDidNotReflow: JSON.stringify(paneBefore) === JSON.stringify(paneAfter),
    paneBefore, paneAfter,
    closedBeforeFocusProbe: beforeFocus === 0,
    focusAloneDoesNotOpen: openedOnFocusAlone === 0,
    arrowDownOpens: arrowOpen === 1,
    arrowDownLandsOnRow: focusedRow,
    secondArrowMovedTo: afterArrow,
    escapeDismisses: afterEscape === 0,
    escapeReturnsFocusToTrigger: focusReturnedToTrigger,
    ...probe,
  }, null, 2)}`)

  await context.close()   // flushes the video
  await browser.close()
  srv.close()
  console.log('wrote frames + video to', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
