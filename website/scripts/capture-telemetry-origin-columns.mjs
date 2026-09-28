/**
 * Screenshot evidence for the two session-origin columns on the Telemetry page.
 *
 * The page derives that dimension twice and the two answers differ on purpose:
 * Spend's **Origin** column comes from the session key, Context's **Turn
 * surface** column comes from the token row that recorded the turn. Each header
 * carries a tip saying so, and a tip is a hover surface -- a diff cannot show
 * where it sits against the sort chevron, or whether the header cell survives
 * the responsive widths the `hide` classes target.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server,
 * with `stubDashboardApi` answering the app shell and one route override
 * carrying the telemetry payload. The payload is a FIXTURE, not measured data:
 * these frames are about labels and layout, and the fixture is shaped to put the
 * interesting values on screen at once -- a background session next to named
 * transports on Spend, and `monitor` plus `webhook` on Context, the two row
 * surfaces the channel taxonomy has no bucket for.
 *
 * Frames:
 *   01-spend-headers           Spend session table at rest: the Origin column and
 *                              its "all background" value, header legible
 *   02-spend-origin-tip        the same header with its tip open
 *   03-spend-grouped-origin    Spend grouped by Origin: the "all background" row
 *   04-context-headers         Context table at rest: the Turn surface column,
 *                              with `monitor` and `webhook` among its values
 *   05-context-turn-surface    the same header with its tip open
 *   06-spend-origin-narrow     the Spend header at 960px, one breakpoint above
 *                              the width that drops the column
 *
 * A tip opens OVER the header it explains, so the at-rest frames are what show
 * the label and the open ones are what show the sentence.
 *
 * Usage: node scripts/capture-telemetry-origin-columns.mjs
 *        OUT=<dir> overrides the output directory.
 */
import { mkdirSync } from 'node:fs'

import { chromium } from 'playwright'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.env.OUT || '/tmp/shots/telemetry-origin-columns'
mkdirSync(OUT, { recursive: true })

const row = (name, credits, turns, share_pct) => ({
  name,
  credits,
  turns,
  per_turn: Math.round((credits / turns) * 10) / 10,
  share_pct,
})

const TELEMETRY = {
  enabled: true,
  window_days: 14,
  shard_count: 6,
  metrics_dir: '/var/lib/kirocrew/metrics',
  startup: null,
  turn: null,
  other: [],
  cost: {
    window_days: 14,
    credits: 12850,
    turns: 1187,
    per_turn: 10.8,
    prior_credits: 9400,
    prior_turns: 910,
    prior_per_turn: 10.3,
    delta_pct: 36.7,
    priciest: { credits: 658, slot: 'chat-41-1785445181', ts: '2026-09-22T09:14:02Z' },
    by_model: [row('claude-opus-5', 9100, 720, 70.8), row('claude-haiku-4.5', 3750, 467, 29.2)],
    by_channel: [row('dashboard', 7400, 520, 57.6), row('cron', 3200, 430, 24.9), row('slack', 2250, 237, 17.5)],
    // `bg` renders through the coined label; the transports render verbatim.
    by_category: [row('dashboard', 7400, 520, 57.6), row('bg', 3200, 430, 24.9), row('slack', 2250, 237, 17.5)],
    context_bands: [
      { label: '0-200k', turns: 640, mean_credits: 6.2 },
      { label: '200-400k', turns: 390, mean_credits: 12.4 },
    ],
    conversations: [
      {
        slot: 'chat-41-1785445181',
        category: 'dashboard',
        channel: 'dashboard',
        title: 'Telemetry column taxonomy',
        credits: 5200,
        turns: 310,
        peak_pct: 62,
        span_days: 4,
        first_ts: 1785445181,
        growth_pct_per_turn: 0.18,
        turns_to_compaction: 41,
      },
      {
        slot: 'cron:default:nightly',
        category: 'bg',
        channel: 'cron',
        credits: 3200,
        turns: 430,
        peak_pct: 38,
        span_days: 14,
        first_ts: 1784445181,
        growth_pct_per_turn: 0.04,
        turns_to_compaction: null,
      },
      {
        slot: 'slack:1712793600.123456',
        category: 'slack',
        channel: 'slack',
        title: 'release thread',
        credits: 2250,
        turns: 237,
        peak_pct: 51,
        span_days: 9,
        first_ts: 1784945181,
        growth_pct_per_turn: 0.11,
        turns_to_compaction: 88,
      },
    ],
    conversation_count: 3,
    navigable_category: 'dashboard',
  },
  context: {
    turns: 1187,
    p50_pct: 34,
    p90_pct: 71,
    max_pct: 92,
    window_days: 14,
    // `monitor` and `webhook` are row surfaces with no Spend bucket of their own,
    // which is the reading the two tips exist to explain.
    sessions: [
      { slot: 'chat-41-1785445181', turns: 310, peak_pct: 62, used: 620000, window: 1000000, agent: 'kirocrew', model: 'claude-opus-5', surface: 'dashboard', ts: '2026-09-24T12:41:00Z' },
      { slot: '_hb', turns: 430, peak_pct: 38, used: 380000, window: 1000000, agent: 'kirocrew-heartbeat', model: 'claude-haiku-4.5', surface: 'heartbeat', ts: '2026-09-24T12:39:00Z' },
      { slot: 'chat-12-1785440000', turns: 96, peak_pct: 44, used: 440000, window: 1000000, agent: 'kirocrew-worker', model: 'claude-opus-5', surface: 'monitor', ts: '2026-09-24T12:30:00Z' },
      { slot: 'hook:review-42', turns: 18, peak_pct: 21, used: 210000, window: 1000000, agent: 'kirocrew', model: 'claude-haiku-4.5', surface: 'webhook', ts: '2026-09-24T11:58:00Z' },
    ],
  },
}

/** Click the tip button inside the header whose sort control carries `label`. */
async function openHeaderTip(page, label) {
  const th = page
    .locator('th')
    .filter({ has: page.getByRole('button', { name: label, exact: true }) })
    .first()
  await th.getByRole('button').nth(1).click()
  await page.getByRole('tooltip').first().waitFor()
  await page.waitForTimeout(250)
}

async function openTelemetry(page, base) {
  await page.goto(base + '/developer?tab=telemetry', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1560, height: 1100 },
    // The tables are 11.5px mono; a 1x shot renders soft when shared.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await stubDashboardApi(page)
  await page.route('**/api/telemetry/startup', route => json(route, TELEMETRY))

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    process.stdout.write(`wrote ${OUT}/${name}.png\n`)
  }

  await openTelemetry(page, base)
  await shot('01-spend-headers')
  await openHeaderTip(page, 'Origin')
  await shot('02-spend-origin-tip')

  await page.keyboard.press('Escape')
  await page.mouse.click(10, 10)
  // The group-by SEGMENT, not the column's sort control: both are buttons named
  // "Origin" and the segmented control comes first in the DOM. Clicking the
  // header instead only re-sorts the session table, which is a different frame.
  await page.getByRole('button', { name: 'Origin', exact: true }).first().click()
  await page.waitForTimeout(600)
  await shot('03-spend-grouped-origin')

  await openTelemetry(page, base)
  await page.getByRole('button', { name: /^Context/ }).first().click()
  await page.waitForTimeout(900)
  await shot('04-context-headers')
  await openHeaderTip(page, 'Turn surface')
  await shot('05-context-turn-surface')

  await page.setViewportSize({ width: 960, height: 1000 })
  await openTelemetry(page, base)
  await shot('06-spend-origin-narrow')

  await browser.close()
  srv.close()
}

main().catch(err => {
  process.stderr.write(`${err}\n`)
  process.exit(1)
})
