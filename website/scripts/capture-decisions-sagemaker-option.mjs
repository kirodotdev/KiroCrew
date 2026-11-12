/**
 * Screenshot harness for the Decision model picker's SageMaker option and for the
 * options a fleet ceiling refuses (Settings > Developer > Feature Previews > Decisions).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and answers
 * `/api/decisions/provider` the way the gateway does on two ceilings, so no endpoint
 * is called. Frames, each in light and dark:
 *   decisions-sagemaker-selected-<theme>.png  a saved SageMaker endpoint shown as
 *                                             SageMaker with its name and region.
 *   decisions-hosted-hidden-<theme>.png       a ceiling that allows only SageMaker
 *                                             runtime hosts and loopback: no hosted
 *                                             default is offered, SageMaker and the
 *                                             local presets are.
 *   decisions-refused-default-no-model-<theme>.png
 *                                             the whole card when the ceiling refuses
 *                                             the hosted default: "No model", the line
 *                                             naming the refusal, the switch drawn off,
 *                                             and no "Sent to" line.
 *   decisions-sagemaker-unpermitted-<theme>.png
 *                                             the hosted default refused and no SageMaker
 *                                             runtime host permitted (`sagemaker_permitted`
 *                                             false): neither hosted option is offered.
 *
 * Each frame asserts the text that makes it that state before it is written.
 *
 * Usage: node scripts/capture-decisions-sagemaker-option.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decisions-sagemaker-option'
mkdirSync(OUT, { recursive: true })

const laya = {
  id: 'laya', name: 'Laya', model: 'english', default_port: 8104, jev_relative_pct: 67, hard_relative_pct: 47,
  peak_ram_gb: 6, recommended_total_ram_gb: 24, p50_secs: 0.17, p95_secs: 0.51, timeout_ms: 2000,
  download_bytes: 846207419, installed: false,
}
const idle = { preset: '', state: 'idle', port: 0, bytes_done: 0, bytes_total: 0, error: '' }
const HOSTED = 'https://api.typesafe.ai/v1/systemone'
const SAGEMAKER = { endpoint_name: 'decision-model-a', region: 'us-west-2', profile: '' }

const FRAMES = [
  {
    name: 'decisions-sagemaker-selected',
    provider: { active: 'sagemaker', configured_endpoint: 'sagemaker://decision-model-a?region=us-west-2', hosted_permitted: true, sagemaker: SAGEMAKER },
    shows: ['Your SageMaker endpoint', 'Endpoint name', 'AWS region'],
    absent: [],
  },
  {
    name: 'decisions-hosted-hidden',
    provider: { active: 'none', configured_endpoint: 'none', hosted_permitted: false, sagemaker: null },
    shows: ['Your SageMaker endpoint', 'Laya', 'No model'],
    absent: [/Jev, hosted by TypeSafe/],
  },
  {
    name: 'decisions-refused-default-no-model',
    // The gateway reports `active: "none"` while configured_endpoint still names the
    // refused hosted default; the card must not present it as the destination.
    provider: { active: 'none', configured_endpoint: HOSTED, hosted_permitted: false, sagemaker: null },
    shows: ['No model'],
    absent: [/^Jev, hosted by TypeSafe/],
    card: true,
    switchOff: true,
    consent: { enabled: false, permits: false },
    cardShows: ["Your fleet's policy blocks Jev, hosted by TypeSafe"],
    cardAbsent: [/^Sent to/, /sent over the internet to Jev/, /No decision model is chosen/],
  },
  {
    name: 'decisions-sagemaker-unpermitted',
    provider: { active: 'none', configured_endpoint: 'none', hosted_permitted: false, sagemaker_permitted: false, sagemaker: null },
    shows: ['Laya', 'No model'],
    absent: [/Jev, hosted by TypeSafe/, /Your SageMaker endpoint/],
  },
]

const providerBody = frame => ({
  presets: [laya], runtime: idle, loopback: false, local_permitted: true,
  sagemaker_available: true, sagemaker_permitted: true, ...frame.provider,
})

const routes = (frame, body) => ({
  '/api/decisions/provider': body,
  '/api/decisions/consent': {
    enabled: true, permits: true, ...frame.consent,
    endpoint: body.configured_endpoint, configured_endpoint: body.configured_endpoint,
    tool_args: false, compaction: false, history_budget_chars: 0, points: [],
  },
  '/api/dashboard/config': { restore_sessions: false, decisions_enabled: true },
  '/api/system': { mem_total_gb: 48 },
  '/api/secrets': { names: [] },
  '/api/config/kirocrew': { ...KIROCREW_CONFIG_FIXTURE, decisions: { bucket: 100, provider: { endpoint: body.configured_endpoint } } },
})

async function capture(browser, base, frame, theme) {
  const context = await browser.newContext({ viewport: { width: 1360, height: 2200 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  const table = routes(frame, providerBody(frame))
  await stubDashboardApi(page, {
    theme,
    localStorageEntries: { 'mc-dev-mode': '1' },
    extra: async (path, route) => {
      if (!(path in table)) return false
      await json(route, table[path])
      return true
    },
  })
  await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
  const picker = page.getByRole('radiogroup', { name: 'Decision model' })
  await picker.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(600)
  for (const text of frame.shows) {
    await picker.getByText(text).first().waitFor({ state: 'visible', timeout: 5000 }).catch(() => {
      throw new Error(`${frame.name}: ${text} is not shown`)
    })
  }
  for (const pattern of frame.absent) {
    if ((await picker.getByText(pattern).count()) > 0) throw new Error(`${frame.name}: ${pattern} is offered`)
  }
  let target = picker
  if (frame.card) {
    target = page.locator('[data-settings-card]').filter({ has: picker })
    for (const text of frame.cardShows) {
      await target.getByText(text).first().waitFor({ state: 'visible', timeout: 5000 }).catch(() => {
        throw new Error(`${frame.name}: ${text} is not shown`)
      })
    }
    if (frame.switchOff) {
      const sw = target.getByRole('switch').first()
      if ((await sw.getAttribute('aria-checked')) !== 'false') throw new Error(`${frame.name}: the switch is drawn on`)
    }
    for (const pattern of frame.cardAbsent) {
      if ((await target.getByText(pattern).count()) > 0) throw new Error(`${frame.name}: ${pattern} is shown`)
    }
  }
  await target.scrollIntoViewIfNeeded()
  await target.screenshot({ path: `${OUT}/${frame.name}-${theme}.png` })
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  for (const frame of FRAMES) {
    for (const theme of ['light', 'dark']) await capture(browser, base, frame, theme)
  }
  await browser.close()
  srv.close()
  console.log(`wrote ${FRAMES.length * 2} shot(s) to ${OUT}`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
