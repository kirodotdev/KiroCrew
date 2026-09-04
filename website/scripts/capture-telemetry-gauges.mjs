/**
 * Screenshot harness for the Telemetry panel's new Gauges section.
 *
 * The PR adds kirocrew.process.* observable gauges; the API reports them as
 * {kind: "gauge", latest} and the panel renders them under a Gauges heading
 * (previously every non-histogram row was folded under Counters and displayed
 * as 0). This captures the Instruments card with realistic gauge + counter +
 * histogram data so the PR shows the rendered delta.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — no gateway, no token. Same technique as capture-i18n-labels.mjs.
 *
 * Usage: node scripts/capture-telemetry-gauges.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/process-gauges'
const PROJECT = '/home/user/.kiro/crew/workspace'
const VIEW = { width: 1500, height: 1000 }

mkdirSync(OUT, { recursive: true })

const stat = (over = {}) => ({
  count: 812, mean_ms: 96.4, p50_ms: 74, p90_ms: 210, min_ms: 8, max_ms: 1450,
  other_generations: 0, total_count: 812, ...over,
})

const telemetry = {
  enabled: true,
  window_days: 14,
  shard_count: 42,
  metrics_dir: '/home/user/.kiro/crew/metrics',
  startup: {
    overall: stat({ count: 63 }), cold: stat({ count: 21 }), warm: stat({ count: 42 }),
    outcome: { ready: 61, error: 2 },
    daily: [],
    distribution: { buckets: [2, 31, 22, 8], bounds: [1500, 3000, 6000] },
    phases: [],
  },
  turn: { ...stat({ count: 480 }), outcome: { ok: 471, error: 9 }, fault_rate: 0.0188 },
  context: null,
  cost: null,
  other: [
    { name: 'kirocrew.mcp.backend.acquire.duration', kind: 'histogram', ...stat({ count: 1134 }) },
    { name: 'kirocrew.mcp.warm_pool.acquire', kind: 'counter', total: 1134,
      by_attr: { 'result=hit': 1031, 'result=miss': 103 } },
    // The new process gauges — point-in-time readings, `latest` not `total`.
    { name: 'kirocrew.process.threads.python', kind: 'gauge', latest: 49, by_attr: {} },
    { name: 'kirocrew.process.threads.os', kind: 'gauge', latest: 96, by_attr: {} },
    { name: 'kirocrew.process.open_fds', kind: 'gauge', latest: 144, by_attr: {} },
    // Multi-process window: the API keys gauge samples per exporting PID so
    // no process masquerades as another; the panel renders the breakdown.
    { name: 'kirocrew.process.memory.rss_bytes', kind: 'gauge', latest: 4402341888,
      by_attr: { 'pid=5346': 4402341888, 'pid=9121': 287309824 } },
    { name: 'kirocrew.process.memory.peak_rss_bytes', kind: 'gauge', latest: 4617089024, by_attr: {} },
    { name: 'kirocrew.process.cpu.seconds', kind: 'counter', total: 5123.4, by_attr: {} },
  ],
}

const FIXED_API = makeFixedApi(PROJECT)

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/telemetry/startup') return json(route, telemetry)
    if (path === '/api/chat/slots') return json(route, [])
    return handleBootRoute(route, path, { project: PROJECT, theme: 'dark', fixedApi: FIXED_API })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-yolo-ack', '1')
  })
  await page.goto(base + '/developer', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  // Open the Telemetry tab (label from the catalog-backed tab strip).
  await page.getByText('Telemetry', { exact: false }).first().click()
  await page.waitForTimeout(1500)

  // The Instruments card carries histograms + Counters + the new Gauges section.
  const gauges = page.getByText('kirocrew.process.threads.os')
  await gauges.waitFor({ timeout: 10000 })
  const card = page.locator('text=kirocrew.process.threads.os').locator(
    'xpath=ancestor::div[contains(@class,"mb-4")][1]',
  )
  await card.screenshot({ path: `${OUT}/instruments-with-gauges.png` })
  await page.screenshot({ path: `${OUT}/telemetry-panel-full.png` })
  console.log('captured:', OUT)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
