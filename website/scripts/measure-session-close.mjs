// Measure session close cost by row position on the built SPA, using the
// gateway-free fixture harness. Reports click-to-removal, long tasks, style
// recalculation, and layout work. Absolute timings vary by host; compare ratios
// within one run.
//
// Usage:
//   npm run build
//   SESSIONS=163 ITER=8 OUT_DIR="$KIROCREW_SCRATCH/session-close-perf" \
//     node scripts/measure-session-close.mjs
//   # Optional: RECORD_OUT=<dir> records the first middle-row sample as WebM.
import { chromium } from 'playwright'
import { mkdirSync, renameSync, writeFileSync } from 'fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.env.OUT_DIR || `${process.env.TMPDIR || '.'}/session-close-perf`
const SESSIONS = Number(process.env.SESSIONS || 163)
const ITER = Number(process.env.ITER || 8)
const RECORD_OUT = process.env.RECORD_OUT || ''
mkdirSync(OUT, { recursive: true })
if (RECORD_OUT) mkdirSync(RECORD_OUT, { recursive: true })

const mkSlots = (n) => Array.from({ length: n }, (_, i) => ({
  key: `chat-1-${String(i).padStart(3, '0')}`,
  title: `Session ${i} — demo work item`,
  running: false,
  messages: 3 + (i % 9),
  agent: i % 3 === 0 ? 'kirocrew' : 'kirocrew-lite',
  last_ts: new Date(Date.now() - (i + 1) * 60_000).toISOString(),
}))

const quantile = (xs, q) => {
  const sorted = [...xs].sort((a, b) => a - b)
  if (!sorted.length) return 0
  const pos = (sorted.length - 1) * q
  const lo = Math.floor(pos), hi = Math.ceil(pos)
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
const positions = {
  top: 0,
  middle: Math.floor(SESSIONS / 2),
  bottom: SESSIONS - 1,
}
const samples = []

for (const [position, index] of Object.entries(positions)) {
  for (let iteration = 0; iteration < ITER; iteration++) {
    const recordThis = RECORD_OUT !== '' && position === 'middle' && iteration === 0
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      reducedMotion: process.env.REDUCED_MOTION === '1' ? 'reduce' : 'no-preference',
      ...(recordThis ? { recordVideo: { dir: RECORD_OUT, size: { width: 1440, height: 900 } } } : {}),
    })
    const page = await ctx.newPage()
    const slots = mkSlots(SESSIONS)
    await stubDashboardApi(page, {
      slots,
      folders: [],
      extra: async (path, route) => {
        if (route.request().method() === 'DELETE' && path.startsWith('/api/chat/slots/')) {
          await json(route, { ok: true })
          return true
        }
        return false
      },
    })
    await page.goto(base)
    await page.waitForSelector('[data-slot-key]', { timeout: 20_000 })
    await page.waitForTimeout(700)

    const key = slots[index].key
    const row = page.locator(`[data-slot-key="${key}"]`)
    await row.locator('.session-row').hover()
    await page.waitForTimeout(50)

    const cdp = await ctx.newCDPSession(page)
    await cdp.send('Performance.enable')
    const metrics = async () => {
      const { metrics } = await cdp.send('Performance.getMetrics')
      const get = (name) => metrics.find(metric => metric.name === name)?.value ?? 0
      return { recalc: get('RecalcStyleCount'), layout: get('LayoutCount') }
    }
    await page.evaluate(() => {
      window.__sessionCloseLongTasks = []
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) window.__sessionCloseLongTasks.push(entry.duration)
      }).observe({ entryTypes: ['longtask'] })
    })
    const before = await metrics()
    const t0 = await page.evaluate(() => performance.now())
    await row.getByLabel('Close session').click()
    const t1 = await page.evaluate(async slotKey => {
      await new Promise(resolve => {
        const check = () => {
          if (!document.querySelector(`[data-slot-key="${slotKey}"]`)) resolve()
        }
        const observer = new MutationObserver(check)
        observer.observe(document.body, { childList: true, subtree: true })
        check()
      })
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      return performance.now()
    }, key)
    await page.waitForTimeout(600)
    const after = await metrics()
    const longTasks = await page.evaluate(() => window.__sessionCloseLongTasks)
    samples.push({
      position,
      iteration,
      clickToRemovedMs: t1 - t0,
      longTaskMs: longTasks.reduce((total, duration) => total + duration, 0),
      longTaskCount: longTasks.length,
      styleRecalcs: after.recalc - before.recalc,
      layouts: after.layout - before.layout,
    })
    const video = recordThis ? page.video() : null
    await ctx.close()
    if (video) renameSync(await video.path(), `${RECORD_OUT}/session-close-middle.webm`)
  }
}

const summary = {}
for (const position of Object.keys(positions)) {
  const rows = samples.filter(sample => sample.position === position)
  summary[position] = {}
  for (const key of ['clickToRemovedMs', 'longTaskMs', 'longTaskCount', 'styleRecalcs', 'layouts']) {
    const values = rows.map(row => row[key])
    summary[position][key] = {
      median: +quantile(values, 0.5).toFixed(1),
      p95: +quantile(values, 0.95).toFixed(1),
    }
  }
}
const report = {
  sessions: SESSIONS,
  iterations: ITER,
  reducedMotion: process.env.REDUCED_MOTION === '1',
  chromium: browser.version(),
  summary,
  samples,
}
const outputPath = `${OUT}/session-close-${SESSIONS}${report.reducedMotion ? '-reduced-motion' : ''}.json`
writeFileSync(outputPath, JSON.stringify(report, null, 2))
console.log(JSON.stringify(report, null, 2))
console.log('DONE', outputPath)

await browser.close()
srv.close()
