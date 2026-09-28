// Momentum-scroll reproduction rig for Kiro Crew chat (iOS-like flings).
//
// Why this exists: scrollTop-step probes CANNOT reproduce the mobile bounce —
// they never coast, so programmatic scroll writes (prepend compensation,
// height sync) never race an in-flight fling. This rig drives REAL touch
// gestures through CDP Input.synthesizeScrollGesture with velocity, which
// makes Chromium produce genuine momentum, then measures content-anchor
// integrity at rAF resolution DURING the coast.
//
// Usage: node momentum_rig.mjs <url>
// Prints a JSON report: per-fling anchor deviations, long tasks, and the
// gesture phase (touch/coast/settled) each deviation landed in.

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
  const rig = (window.__rig = { events: [], longTasks: [], phase: 'idle', frames: 0 })
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
  const tick = () => {
    rig.frames++
    const st = el.scrollTop
    const scrolled = st - lastScrollTop
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

/** One fling: touch-drag with velocity so Chromium coasts afterward. */
async function fling(direction, speed) {
  await page.evaluate((ph) => { window.__rig.phase = ph }, 'touch')
  await cdp.send('Input.synthesizeScrollGesture', {
    x: 195, y: 500,
    xDistance: 0,
    yDistance: direction === 'up' ? 600 : -600, // up = view older (content down)
    speed,               // px/s — high speed triggers fling/momentum
    gestureSourceType: 'touch',
  })
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
  return {
    frames: rig.frames,
    totalJumps: rig.events.length,
    byPhase,
    worst,
    longTasks: { n: lt.length, worst: lt.sort((a, b) => b.d - a.d).slice(0, 5) },
    error: rig.error,
  }
})
console.log(JSON.stringify(report, null, 1))
await browser.close()
