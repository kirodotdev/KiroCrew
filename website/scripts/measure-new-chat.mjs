// Measure "New Chat click → new session row painted" on the built SPA against
// the repo's gateway-free fixture harness (serve-dist + stub-dashboard-api; no
// gateway, no token). Companion to capture-sidebar-perf.mjs: that script
// proves the sidebar RENDERS at scale, this one prices the most common
// mutation of it.
//
// Reports median / p95 over warm iterations of:
//   clickToRowMs   — click dispatched → the returned slot key's row is in the
//                    DOM and two animation frames have passed (crossed paint)
//   longTaskMs     — summed PerformanceObserver 'longtask' durations in that
//                    window (main-thread blocking the user can feel)
//   styleRecalcs / layouts — CDP Performance.getMetrics deltas
//                    (RecalcStyleCount / LayoutCount), the browser-side cost
//                    that scales with how many rows the commit dirtied
//
// Usage:
//   npm run build
//   SESSIONS=163 ITER=20 OUT_DIR="$KIROCREW_SCRATCH/new-chat-perf" \
//     node scripts/measure-new-chat.mjs
//
// The stub answers POST /api/chat/slots with a fresh slot and appends it to
// the GET fixture; the dashboard's websocket is swallowed by the harness, so
// the row arrives via the create response alone — one code path, no
// broadcast timing to average away. Absolute milliseconds vary by host; the
// intended reading is the before/after RATIO on one machine
// (website/docs/testing.md prefers ratios over wall time).
import { chromium } from 'playwright'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { mkdirSync, writeFileSync } from 'fs'

const OUT = process.env.OUT_DIR || '/tmp/new-chat-perf'
const SESSIONS = Number(process.env.SESSIONS || 163)
const ITER = Number(process.env.ITER || 20)
const WARMUP = Number(process.env.WARMUP || 5)
mkdirSync(OUT, { recursive: true })

const mkSlots = (n) => Array.from({ length: n }, (_, i) => ({
  key: `chat-1-${String(i).padStart(3, '0')}`,
  title: `Session ${i} — demo work item`,
  running: i % 17 === 0,
  messages: 3 + (i % 9),
  agent: i % 3 === 0 ? 'kirocrew' : 'kirocrew-lite',
  last_ts: new Date(Date.now() - (i + 1) * 3600_000).toISOString(),
}))

const quantile = (xs, q) => {
  const s = [...xs].sort((a, b) => a - b)
  if (!s.length) return 0
  const pos = (s.length - 1) * q
  const lo = Math.floor(pos), hi = Math.ceil(pos)
  return s[lo] + (s[hi] - s[lo]) * (pos - lo)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  // Deterministic: the harness prices the commit, not the spring. Row layout
  // animation is gated off under reduced motion, so run BOTH settings when
  // the change under test is the animation itself.
  reducedMotion: process.env.REDUCED_MOTION === '1' ? 'reduce' : 'no-preference',
})
const page = await ctx.newPage()

const slots = mkSlots(SESSIONS)
let created = 0
await stubDashboardApi(page, {
  slots,
  folders: [],
  extra: async (path, route) => {
    if (path === '/api/chat/slots' && route.request().method() === 'POST') {
      const slot = {
        key: `chat-9-${String(created++).padStart(3, '0')}`,
        title: 'New chat', running: false, messages: 0, agent: 'kirocrew',
        last_ts: new Date().toISOString(),
      }
      slots.unshift(slot)
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(slot) })
      return true
    }
    return false
  },
})

await page.goto(base)
await page.waitForSelector('[data-slot-key]', { timeout: 20000 })
await page.waitForTimeout(800)

const cdp = await ctx.newCDPSession(page)
await cdp.send('Performance.enable')
const metrics = async () => {
  const { metrics } = await cdp.send('Performance.getMetrics')
  const get = (n) => metrics.find(m => m.name === n)?.value ?? 0
  return { recalc: get('RecalcStyleCount'), layout: get('LayoutCount') }
}

// Long-task observer lives across iterations; each iteration reads and clears.
await page.evaluate(() => {
  const w = window
  w.__lt = []
  new PerformanceObserver(list => { for (const e of list.getEntries()) w.__lt.push(e.duration) })
    .observe({ entryTypes: ['longtask'] })
})

const newChat = page.getByRole('button', { name: /new chat session/i }).first()
const samples = []
for (let i = 0; i < WARMUP + ITER; i++) {
  const before = await metrics()
  await page.evaluate(() => { window.__lt = [] })
  const expectKey = `chat-9-${String(created).padStart(3, '0')}`
  const t0 = await page.evaluate(() => performance.now())
  await newChat.click()
  // Row present, then two rAFs so the measurement crosses the paint that
  // shows it — a MutationObserver alone would stop before layout/paint.
  const t1 = await page.evaluate(async (key) => {
    await new Promise(resolve => {
      const check = () => document.querySelector(`[data-slot-key="${key}"]`) && resolve()
      const mo = new MutationObserver(check)
      mo.observe(document.body, { childList: true, subtree: true })
      check()
    })
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))
    return performance.now()
  }, expectKey)
  // Let the (optional) spring settle so the next iteration starts idle and
  // this iteration's style/layout deltas include the animation tail.
  await page.waitForTimeout(600)
  const after = await metrics()
  const longTasks = await page.evaluate(() => window.__lt)
  const sample = {
    clickToRowMs: t1 - t0,
    longTaskMs: longTasks.reduce((a, b) => a + b, 0),
    longTaskCount: longTasks.length,
    styleRecalcs: after.recalc - before.recalc,
    layouts: after.layout - before.layout,
    rows: await page.locator('[data-slot-key]').count(),
  }
  if (i >= WARMUP) samples.push(sample)
}

const summary = {}
for (const k of ['clickToRowMs', 'longTaskMs', 'longTaskCount', 'styleRecalcs', 'layouts']) {
  const xs = samples.map(s => s[k])
  summary[k] = { median: +quantile(xs, 0.5).toFixed(1), p95: +quantile(xs, 0.95).toFixed(1) }
}
const report = {
  sessions: SESSIONS, iterations: ITER, warmup: WARMUP,
  reducedMotion: process.env.REDUCED_MOTION === '1',
  chromium: browser.version(),
  summary, samples,
}
writeFileSync(`${OUT}/new-chat-perf.json`, JSON.stringify(report, null, 2))
console.log(JSON.stringify({ sessions: SESSIONS, reducedMotion: report.reducedMotion, chromium: report.chromium, ...summary }, null, 2))
console.log('DONE', `${OUT}/new-chat-perf.json`)

await ctx.close()
await browser.close()
srv.close()
