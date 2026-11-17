/**
 * Screenshot harness for the Decisions (Jev) card under the administrator's ceiling
 * (Settings > Developer > Feature Previews).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and answers
 * `/api/dashboard/config`, `/api/decisions/consent` and `/api/decisions/provider` the
 * way the gateway does for each state, so no gateway is needed. Frames (light theme):
 *   all-allowed       no ceiling; the hosted provider chosen, its entry open with its
 *                     API key field.
 *   allow-list-one    the ceiling's `providers` list allows Laya only: every other
 *                     model greyed with the administrator's wording.
 *   admin-disabled    `capabilities.decisions` off: the card's plain notice.
 *   sagemaker-active  a SageMaker endpoint answering.
 *   local-active      a local model (Laya) running and answering.
 *   all-models-refused  the `providers` list allows none: every option greyed but
 *                     "No model", and the sentence saying so above them.
 *   preset-refused-consent-on  a configured local model the list refuses, with a consent
 *                     recorded for it: the refusal under "Sent to", the egress still said,
 *                     and no "agreed to a different address" warning.
 *   hosted-default-refused  the hosted default saved and refused: no option selected,
 *                     the hosted entry tagged "Saved, not allowed" with Clear from settings.
 *
 * The same script runs against an older build for the "before" frames: pass
 * `--before`, which keeps the crop and drops the assertions only the new card meets.
 *
 * Usage: node scripts/capture-decision-model-governance.mjs [outDir] [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi, KIROCREW_CONFIG_FIXTURE } from './lib/stub-dashboard-api.mjs'

const BEFORE = process.argv.includes('--before')
const OUT = process.argv.slice(2).find(a => !a.startsWith('--')) || '../temp-screenshots/decision-model-governance/after'
mkdirSync(OUT, { recursive: true })

const HOSTED = 'https://api.typesafe.ai/v1/systemone'
const LAYA_URL = 'http://127.0.0.1:8104/v1/systemone'
const SAGEMAKER_URL = 'sagemaker://decision-model-a?region=us-west-2'
const laya = {
  id: 'laya', name: 'Laya', model: 'english', default_port: 8104, jev_relative_pct: 67, hard_relative_pct: 47,
  peak_ram_gb: 6, recommended_total_ram_gb: 24, p50_secs: 0.17, p95_secs: 0.51, timeout_ms: 2000,
  download_bytes: 846207419, installed: true,
}
const plumb = {
  id: 'plumb-4b', name: 'Plumb-4B', model: 'plumb-4b', default_port: 8102, jev_relative_pct: 103,
  hard_relative_pct: 109, peak_ram_gb: 12, recommended_total_ram_gb: 48, p50_secs: 1.4, p95_secs: 4.2,
  timeout_ms: 5000, download_bytes: 8300000000, installed: false,
}
const idle = { preset: '', state: 'idle', port: 0, bytes_done: 0, bytes_total: 0, error: '' }
const allowed = { jev: true, 'plumb-4b': true, laya: true, sagemaker: true }

const ONLY = process.env.FRAMES ? new Set(process.env.FRAMES.split(',')) : null
const FRAMES = [
  {
    name: 'all-allowed',
    provider: { active: 'jev', configured_endpoint: HOSTED },
    shows: ['Jev, hosted by TypeSafe', 'Jev API key'],
  },
  {
    name: 'allow-list-one',
    provider: {
      active: 'laya', configured_endpoint: LAYA_URL, loopback: true,
      runtime: { preset: 'laya', state: 'running', port: 8104, bytes_done: 0, bytes_total: 0, error: '' },
      hosted_permitted: false, sagemaker_permitted: false,
      provider_permitted: { jev: false, 'plumb-4b': false, laya: true, sagemaker: false },
    },
    shows: ['Not allowed by your administrator.'],
  },
  {
    name: 'admin-disabled',
    dashboard: { decisions_enabled: false },
    provider: { active: 'jev', configured_endpoint: HOSTED },
    notice: true,
    shows: ["Your administrator's policy turns off decision models for this dashboard"],
  },
  {
    name: 'all-models-refused',
    provider: {
      active: 'none', configured_endpoint: 'none', hosted_permitted: false, sagemaker_permitted: false,
      provider_permitted: { jev: false, 'plumb-4b': false, laya: false, sagemaker: false },
    },
    consent: { enabled: false, permits: false },
    shows: ['Your administrator allows none of the listed decision models.', 'No model'],
  },
  {
    // A configured local model the list refuses, over a consent recorded for it: the
    // refusal alone, the egress still said, no "agreed to a different address" warning.
    name: 'preset-refused-consent-on',
    provider: {
      active: 'laya', configured_endpoint: LAYA_URL, loopback: true,
      provider_permitted: { ...allowed, laya: false },
    },
    consent: { enabled: true, permits: false, withdrawn: true },
    shows: ['Sent to', 'Your administrator does not allow the decision model saved in your settings.'],
  },
  {
    name: 'hosted-default-refused',
    provider: {
      active: 'none', configured_endpoint: HOSTED, hosted_permitted: false,
      provider_permitted: { ...allowed, jev: false },
    },
    consent: { enabled: false, permits: false },
    shows: ['Jev, hosted by TypeSafe is still saved in your settings', 'Saved, not allowed', 'Clear from settings'],
  },
  {
    // A settings-search hit on the hosted key, while a local model is in use: the
    // hosted entry is highlighted, nothing opens and no switch is queued.
    name: 'search-highlights-hosted-entry',
    query: '?sub=feature-previews&highlight=developer.jev-api-key',
    provider: {
      active: 'laya', configured_endpoint: LAYA_URL, loopback: true,
      runtime: { preset: 'laya', state: 'running', port: 8104, bytes_done: 0, bytes_total: 0, error: '' },
    },
    shows: ['Jev, hosted by TypeSafe'],
  },
  {
    name: 'sagemaker-active',
    provider: {
      active: 'sagemaker', configured_endpoint: SAGEMAKER_URL,
      sagemaker: { endpoint_name: 'decision-model-a', region: 'us-west-2', profile: '' },
    },
    shows: ['Your SageMaker endpoint', 'Endpoint name'],
  },
  {
    name: 'local-active',
    provider: {
      active: 'laya', configured_endpoint: LAYA_URL, loopback: true,
      runtime: { preset: 'laya', state: 'running', port: 8104, bytes_done: 0, bytes_total: 0, error: '' },
    },
    shows: ['Laya', 'Running on this machine.'],
  },
]

const providerBody = frame => ({
  presets: [plumb, laya], runtime: idle, loopback: false, local_permitted: true, hosted_permitted: true,
  sagemaker_available: true, sagemaker_permitted: true, sagemaker: null, provider_permitted: allowed,
  ...frame.provider,
})

const routes = (frame, body) => ({
  '/api/decisions/provider': body,
  '/api/decisions/consent': {
    enabled: true, permits: true, ...frame.consent,
    endpoint: body.configured_endpoint, configured_endpoint: body.configured_endpoint,
    tool_args: false, compaction: false, memory_text: false, nudge_evidence: false, browser: true,
    browser_images: true, history_budget_chars: 0,
    // The gateway reports every point off while nothing would be sent.
    points: [
      { id: 'skills.select', status: frame.consent?.permits === false ? 'off' : 'active', needs_scope: null },
      { id: 'message.steer', status: frame.consent?.permits === false ? 'off' : 'active', needs_scope: null },
    ],
  },
  '/api/dashboard/config': { restore_sessions: false, decisions_enabled: true, ...frame.dashboard },
  '/api/system': { mem_total_gb: 32 },
  '/api/secrets': { names: ['TYPESAFE_API_KEY'] },
  '/api/config/kirocrew': {
    ...KIROCREW_CONFIG_FIXTURE, decisions: { bucket: 100, provider: { endpoint: body.configured_endpoint } },
  },
})

async function capture(browser, base, frame) {
  const context = await browser.newContext({ viewport: { width: 1360, height: 2400 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  const table = routes(frame, providerBody(frame))
  await stubDashboardApi(page, {
    theme: 'light',
    localStorageEntries: { 'mc-dev-mode': '1' },
    extra: async (path, route) => {
      if (!(path in table)) return false
      await json(route, table[path])
      return true
    },
  })
  await page.goto(base + '/settings/developer' + (frame.query ?? ''), { waitUntil: 'domcontentloaded' })
  // The box the card sits in: the last group of the Feature Previews page, which an
  // older build leaves empty when governance withdraws the card.
  const section = page.locator('[data-setting-key="feature-previews-section"]')
  await section.waitFor({ state: 'visible', timeout: 15000 })
  let target
  if (frame.notice && !BEFORE) {
    target = page.locator('[data-settings-card]').filter({ has: page.getByTestId('decisions-admin-disabled') })
  } else if (frame.notice) {
    // The older build draws no card at all here, and the box that would hold it is
    // empty and hidden: photograph the last group the page does draw, its foot.
    target = section.locator('[data-settings-box]:visible').last()
  } else {
    const picker = page.getByRole('radiogroup', { name: 'Decision model' })
    await picker.waitFor({ state: 'visible', timeout: 15000 })
    target = page.locator('[data-settings-card]').filter({ has: picker })
    if (BEFORE && frame.name === 'all-allowed') {
      // The older card kept the API key in its shared block: open it so it shows.
      await target.getByText('Shared settings', { exact: true }).click()
    }
  }
  await target.first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(500)
  if (!BEFORE) {
    for (const text of frame.shows) {
      await target.getByText(text).first().waitFor({ state: 'visible', timeout: 5000 }).catch(() => {
        throw new Error(`${frame.name}: ${text} is not shown`)
      })
    }
  }
  await target.first().scrollIntoViewIfNeeded()
  await target.first().screenshot({ path: `${OUT}/${frame.name}.png` })
  console.log('wrote', `${OUT}/${frame.name}.png`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const frame of FRAMES) if (!ONLY || ONLY.has(frame.name)) await capture(browser, base, frame)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
