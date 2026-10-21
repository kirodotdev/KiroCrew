// Momentum-scroll reproduction rig for Kiro Crew chat (iOS-like flings).
//
// Why this exists: scrollTop-step probes CANNOT reproduce the mobile bounce —
// they never coast, so programmatic scroll writes (prepend compensation,
// height sync) never race an in-flight fling. This rig drives REAL touch
// drags through CDP Input.dispatchTouchEvent, released while moving so
// Chromium produces genuine momentum, then measures content-anchor integrity
// and frame intervals at rAF resolution DURING the coast.
//
// Input.synthesizeScrollGesture is NOT used: with gestureSourceType 'touch' it
// moves .chat-container 0 px in current headless Chromium, so every metric
// reads as a clean zero. The run therefore fails (exit 1) when the flings
// scrolled less than MIN_SCROLLED_PX in total.
//
// Usage: node momentum_rig.mjs <url> [warmupMs]
// Prints a JSON report: per-fling anchor deviations, long tasks, the gesture
// phase (touch/coast/settled) each landed in, frame-interval percentiles and
// the distance scrolled.

const MIN_SCROLLED_PX = 2000

import { chromium } from 'playwright'

const url = process.argv[2]
if (!url) { console.error('usage: node momentum_rig.mjs <url>'); process.exit(2) }

const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({
  viewport: { width: 390, height: 844 },
  hasTouch: true,
  isMobile: true,
  deviceScaleFactor: 3,
  userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
})
const page = await context.newPage()
await page.goto(url, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(9000) // app boot + transcript load

// rAF-resolution anchor watcher, installed once; reports deviations where the
// content under the viewport center moves DIFFERENTLY from the scroll delta.
await page.evaluate(() => {
  const el = document.querySelector('.chat-container')
  if (!el) { window.__rig = { error: 'no scroller' }; return }
  const rig = (window.__rig = {
    events: [], longTasks: [], phase: 'idle', frames: 0, intervals: [], lastTs: 0, scrolledPx: 0,
  })
  new PerformanceObserver(l => {
    for (const e of l.getEntries()) rig.longTasks.push({ d: Math.round(e.duration), phase: rig.phase })
  }).observe({ entryTypes: ['longtask'] })
  let anchor = null
  let anchorTop = 0
  let lastScrollTop = el.scrollTop
  const pick = () => {
    const rows = [...document.querySelectorAll('[data-display-index]')]
    const mid = el.getBoundingClientRect().top + el.clientHeight / 2
    let best = 1e9; let a = null
    for (const r of rows) {
      const rect = r.getBoundingClientRect()
      const d = Math.abs((rect.top + rect.bottom) / 2 - mid)
      if (d < best) { best = d; a = r }
    }
    return a
  }
  const tick = (ts) => {
    rig.frames++
    // Frame intervals and distance only while a gesture is live: the rig's own
    // jump to the bottom is not a fling and must not make a dead gesture valid.
    const live = rig.phase === 'touch' || rig.phase === 'coast'
    if (rig.lastTs && live) rig.intervals.push(ts - rig.lastTs)
    rig.lastTs = ts
    const st = el.scrollTop
    const scrolled = st - lastScrollTop
    if (live) rig.scrolledPx += Math.abs(scrolled)
    if (!anchor || !anchor.isConnected) {
      anchor = pick()
      anchorTop = anchor ? anchor.getBoundingClientRect().top : 0
    } else {
      const t = anchor.getBoundingClientRect().top
      const contentMoved = t - anchorTop
      // Content should move by exactly -scrolled. Deviation = felt jump.
      const dev = contentMoved + scrolled
      if (Math.abs(dev) > 24) {
        rig.events.push({
          dev: Math.round(dev), phase: rig.phase, st: Math.round(st),
          idx: anchor.dataset.displayIndex, t: Math.round(performance.now()),
        })
        // Re-base so one jump is one event, not a persistent offset.
      }
      anchorTop = t
    }
    lastScrollTop = st
    requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
})

const cdp = await context.newCDPSession(page)

// Drag origin: the scroller's horizontal centre, high enough that a 600 px
// drag stays inside it.
const origin = await page.evaluate(() => {
  const el = document.querySelector('.chat-container')
  if (!el) return null
  const r = el.getBoundingClientRect()
  return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + Math.max(20, (r.height - 600) / 2)) }
})
if (!origin) {
  console.error('momentum_rig: no .chat-container on the page')
  await browser.close()
  process.exit(1)
}

const DRAG_PX = 600
const STEP_MS = 8

/** One fling: a 600 px finger drag at `speed` px/s, released while moving so
 *  Chromium coasts afterward. 'up' views older rows (the finger moves down). */
async function fling(direction, speed) {
  await page.evaluate((ph) => { window.__rig.phase = ph }, 'touch')
  const sign = direction === 'up' ? 1 : -1
  const y0 = direction === 'up' ? origin.y : origin.y + DRAG_PX
  const steps = Math.max(4, Math.round(((DRAG_PX / speed) * 1000) / STEP_MS))
  const pt = (y) => [{ x: origin.x, y: Math.round(y) }]
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: pt(y0) })
  for (let s = 1; s <= steps; s++) {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: pt(y0 + (sign * DRAG_PX * s) / steps) })
    await new Promise((r) => setTimeout(r, STEP_MS))
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  await page.evaluate((ph) => { window.__rig.phase = ph }, 'coast')
  await page.waitForTimeout(900) // momentum decay window
  await page.evaluate((ph) => { window.__rig.phase = ph }, 'settled')
  await page.waitForTimeout(600)
}

// Scenario: park at bottom, then fling upward through history repeatedly with
// reading pauses (the user's rhythm), then a few fast consecutive flings.
await page.evaluate(() => {
  const el = document.querySelector('.chat-container')
  if (el) el.scrollTop = el.scrollHeight
})
// Optional warmup: let the farm + idle prefetch measure the whole transcript
// before the gesture phase (the WARM state a returning reader is in).
const warmupMs = Number(process.argv[3] ?? 0)
if (warmupMs > 0) await page.waitForTimeout(warmupMs)
await page.waitForTimeout(1500)

for (let i = 0; i < 6; i++) {
  await fling('up', 4500)
  await page.waitForTimeout(1200) // reading pause — staging drains, farm may run
}
// Fast burst: consecutive flings with no pause (stress the walk + landings).
for (let i = 0; i < 8; i++) await fling('up', 6500)

const report = await page.evaluate(() => {
  const rig = window.__rig
  const byPhase = {}
  for (const e of rig.events) byPhase[e.phase] = (byPhase[e.phase] ?? 0) + 1
  const worst = [...rig.events].sort((a, b) => Math.abs(b.dev) - Math.abs(a.dev)).slice(0, 6)
  const lt = rig.longTasks.filter(t => t.d > 50)
  const s = [...(rig.intervals ?? [])].sort((a, b) => a - b)
  const q = (p) => (s.length ? Math.round(s[Math.min(s.length - 1, Math.ceil(p * s.length) - 1)] * 10) / 10 : null)
  return {
    frames: rig.frames,
    totalJumps: rig.events.length,
    byPhase,
    worst,
    longTasks: { n: lt.length, worst: lt.sort((a, b) => b.d - a.d).slice(0, 5) },
    frameIntervalMs: { n: s.length, p50: q(0.5), p95: q(0.95), p99: q(0.99), max: q(1) },
    scrolledPx: Math.round(rig.scrolledPx ?? 0),
    error: rig.error,
  }
})
report.valid = !report.error && report.scrolledPx >= MIN_SCROLLED_PX
console.log(JSON.stringify(report, null, 1))
await browser.close()
if (!report.valid) {
  // A gesture that did not scroll measures nothing: zero long tasks and zero
  // jumps would read as a pass.
  console.error(
    `momentum_rig: INVALID run, scrolled ${report.scrolledPx} px (< ${MIN_SCROLLED_PX})` +
      (report.error ? `, ${report.error}` : ''),
  )
  process.exit(1)
}
