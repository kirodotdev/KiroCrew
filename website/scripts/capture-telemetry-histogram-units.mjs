/**
 * Screenshot harness for the Telemetry panel's instrument table, on the two
 * sampled process histograms.
 *
 * Runs the REAL built SPA behind the shared `serveDist` server and answers every
 * /api/** call from fixtures through `stubDashboardApi`. No gateway, no dashboard
 * auth, no kiro-cli. Only the telemetry payload is fixture data: the component,
 * the CSS and the formatters are the shipped ones.
 *
 * The payload carries one row of each family so a reader can compare them in one
 * frame:
 *   kirocrew.process.memory.rss_sampled  unit "By"  -> a byte distribution
 *   kirocrew.process.cpu.utilization     unit "1"   -> a share of the cores
 *   kirocrew.tool.call.duration          no unit    -> the millisecond family
 * plus a zero-sample byte row, which is the case a percentile cannot decide: an
 * empty window reports only `count` and `unit`.
 *
 * Usage: node scripts/capture-telemetry-histogram-units.mjs [outDir] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || './temp-screenshots/telemetry-histogram-units'
const DIST = process.argv[3] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

/** The duration family's stat block, as `_Hist.stats()` names its fields. */
const msStat = (over = {}) => ({
  count: 10, mean_ms: 100, p50_ms: 90, p90_ms: 200, min_ms: 10, max_ms: 300,
  other_generations: 0, total_count: 10, ...over,
})

const TELEMETRY = {
  enabled: true,
  window_days: 14,
  shard_count: 3,
  metrics_dir: '/var/lib/kirocrew/metrics',
  startup: {
    overall: msStat(), cold: msStat(), warm: msStat(),
    outcome: { ready: 10 },
    daily: [],
    distribution: { buckets: [0, 7, 3], bounds: [3000, 5000] },
    phases: [],
  },
  turn: { ...msStat({ count: 80 }), outcome: { ok: 80 }, fault_rate: 0 },
  context: null,
  other: [
    // Resident set, sampled on the adaptive controller's tick. Unit-neutral keys
    // plus a unit, which is what tells this row from a duration.
    {
      name: 'kirocrew.process.memory.rss_sampled',
      kind: 'histogram', unit: 'By',
      count: 1240, total: 1860000000000, mean: 1500000000,
      p50: 1480000000, p90: 3200000000, min: 612000000, max: 4410000000,
      other_generations: 0, total_count: 1240,
    },
    // Share of one machine's cores over the interval between two samples.
    {
      name: 'kirocrew.process.cpu.utilization',
      kind: 'histogram', unit: '1',
      count: 1240, total: 86.8, mean: 0.07,
      p50: 0.0625, p90: 0.41, min: 0.008, max: 0.94,
      other_generations: 0, total_count: 1240,
    },
    // The duration family, unchanged: no unit, millisecond keys.
    {
      name: 'kirocrew.tool.call.duration',
      kind: 'histogram',
      count: 8140, mean_ms: 412, p50_ms: 96, p90_ms: 1180, min_ms: 11, max_ms: 61200,
      other_generations: 0, total_count: 8140,
    },
    // An empty window on a byte instrument: count and unit, no percentile of any
    // name. The unit is the only thing left that says which family it is.
    {
      name: 'kirocrew.process.memory.rss_sampled_idle_shard',
      kind: 'histogram', unit: 'By', count: 0,
    },
    // A gauge and a counter, so the shot shows the table's other two sections are
    // untouched by the row-selection change.
    {
      name: 'kirocrew.process.threads.os',
      kind: 'gauge', latest: 72, by_attr: {},
    },
    {
      name: 'kirocrew.mcp.warm_pool.acquire',
      kind: 'counter', total: 418, by_attr: { 'result=hit': 402, 'result=miss': 16 },
    },
  ],
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1500, height: 1100 }, deviceScaleFactor: 2 })
const page = await context.newPage()

await stubDashboardApi(page, {
  // `telemetry:tab` opens the instrument table directly; the panel defaults to
  // Spend, and clicking through would photograph a transition. Stored raw, which
  // is what `usePersistedString` reads back.
  localStorageEntries: { 'telemetry:tab': 'latency', 'mc-lang': 'en' },
  extra: async (path, route) => {
    if (path === '/api/telemetry/startup') {
      await json(route, TELEMETRY)
      return true
    }
    return false
  },
})

await page.goto(base + '/developer?tab=telemetry', { waitUntil: 'domcontentloaded' })

// Anchored on a row this change is about, so a blank page or an error boundary
// fails the run instead of writing a PNG of one.
await page.getByText('kirocrew.process.memory.rss_sampled', { exact: true })
  .first().waitFor({ state: 'visible', timeout: 20000 })
await page.waitForTimeout(600)

const out = join(OUT, 'instrument-table.png')
await page.screenshot({ path: out, fullPage: true })
console.log('wrote', out)

await context.close()
await browser.close()
srv.close()
