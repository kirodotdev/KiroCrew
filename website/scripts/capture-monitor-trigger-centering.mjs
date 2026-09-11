/**
 * Screenshot harness + geometry check for the COMPOSER AUTOMATION TRIGGER's
 * vertical centring.
 *
 * The bounded-monitor trigger is an `IconButton` — a plain block `<button>` with
 * no flex row — holding an INLINE glyph (`MonitorRadar`'s `.lucide-inline` is
 * `display:inline-block; vertical-align:-0.125em`) and, when a monitor is armed,
 * a probe count beside it. In a block box the glyph lands on the line box's
 * BASELINE rather than at the box's centre, so it rides several px high in the
 * 32px control and the count butts against it with no gap. The legacy goal
 * trigger it replaced never had this: its own button carries
 * `flex items-center gap-1`.
 *
 * This asserts as well as photographs: it measures real bounding boxes in the
 * REAL built SPA (website/dist) and exits non-zero when the glyph's centre is
 * more than CENTRE_MAX px off the button's centre, or when glyph and count are
 * not separated. Nothing in CI runs this file — the CI-enforced half of the
 * invariant is the class assertion in src/test/SessionAutomationPopover.test.tsx.
 *
 * To photograph the off-centre state for a before/after comparison, check the
 * component out at a ref that predates the fix
 * (`git checkout <ref> -- src/components/SessionAutomationPopover.tsx`),
 * `npm run build`, and run this with a different outDir; the armed scenario then
 * reports `centred: false` with a ~4px offset and `gap: 0`.
 *
 * Usage: node scripts/capture-monitor-trigger-centering.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/monitor-trigger-centering'
const SLOT = 'chat-monitor'
const PROJECT = '/home/user/workspace/notes'

/**
 * Glyph centre → button centre, in px at deviceScaleFactor 1 (boundingBox
 * reports CSS px, so the 2x scale factor below does not enter this number).
 *
 * 0.5 allows sub-pixel layout rounding and nothing else: `items-center` centres
 * exactly, and the baseline placement this guards against is ~4px off in a 32px
 * control. Do NOT raise this to accommodate a future glyph — a taller icon still
 * centres; only a non-flex box does not.
 */
const CENTRE_MAX = 0.5

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Watch the review lanes on #123',
  running: false,
  last_message: 'Armed a bounded monitor on the pull request.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

/**
 * One armed structured-monitor record, in the wire shape
 * `/api/monitors/slot/<key>` returns. Mirrors src/test/monitorFixtures.ts; the
 * only fields this harness depends on are the ones that make the trigger render
 * its active treatment and its probe count (`active`, `probe_count`).
 */
const armedMonitor = {
  id: 'loop-1',
  slot_key: SLOT,
  message: 'Address actionable review feedback.',
  idle_secs: 300,
  max_cycles: 0,
  cycle_count: 0,
  active: true,
  last_fire_ts: 0,
  next_due_ts: 1_800_000_300,
  stopped_reason: '',
  monitor: {
    version: 1,
    config_generation: 1,
    kind: 'github_pull_request',
    target: 'https://github.com/owner/repo/pull/123',
    objective: 'review_ready',
    cadence_secs: 300,
    wake_instructions: 'Address actionable review feedback.',
    budgets: {
      max_runtime_secs: 14_400,
      max_agent_turns: 8,
      max_tokens: 250_000,
      max_provider_errors: 3,
    },
    last_observation: {
      blocking_review: 'none',
      checks: { failed: [], passed: ['CI / test', 'lint'], pending: [], unknown: [] },
      draft: false,
      head_revision: '0123456789abcdef0123456789abcdef01234567',
      kind: 'github_pull_request',
      mergeability: 'mergeable',
      review_decision: 'approved',
      review_threads_complete: true,
      state: 'open',
      target: 'github.com/owner/repo#123',
      unresolved_review_threads: 0,
    },
    last_observation_status: 'pending',
    last_observation_reason_code: 'checks_pending',
    last_observed_at: 1_800_000_000,
    last_fingerprint: 'abc',
    last_wake_fingerprint: '',
    wake_in_flight: false,
    wake_delivery: null,
    wake_count: 2,
    completion_evidence_deadline: 0,
    last_completion_fingerprint: '',
    last_completion_disposition: null,
    last_completed_at: 0,
    token_usage_known: true,
    agent_turns: 2,
    input_tokens: 1200,
    output_tokens: 300,
    probe_count: 5,
    provider_error_count: 1,
    consecutive_provider_errors: 0,
    last_probe_at: 1_800_000_000,
    last_decision: 'no_change',
    last_provider_error: null,
    next_probe_at: 1_800_000_300,
    outcome: null,
    stopped_reason: '',
    stopped_at: 0,
  },
}

const detail = {
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Watch PR #123 until it is review-ready.' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Monitoring is active. I will report on an actionable revision.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The control row is dense 11px type; 1x renders the count soft on GitHub.
    deviceScaleFactor: 2,
  })

  /**
   * Routes the shared stub does not know about. Each branch AWAITS `json()` and
   * then returns `true` — the stub reads a falsy return as "not handled" and
   * fulfils the route itself, which would double-fulfil the same route.
   *
   * `armed` decides whether this page has a monitor: the trigger renders in both
   * states, and only the armed one carries the count the gap is measured on.
   */
  const routes = armed => async (path, route) => {
    if (path.startsWith('/api/autonudge/slot/')) {
      await json(route, { enabled: true, loop: null })
      return true
    }
    if (path.startsWith('/api/monitors/slot/')) {
      await json(route, { enabled: true, monitor: armed ? armedMonitor : null })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  let page = null

  async function load(armed) {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme: 'dark',
      extra: routes(armed),
      localStorageEntries: { 'mc-active-slot': SLOT },
    })
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  /**
   * Measure the trigger. Returns null when a piece is missing so the caller
   * fails loudly instead of silently "passing" on an empty page.
   */
  async function measure(armed) {
    const trigger = page.locator('[data-testid="composer-control-row"] button').filter({
      has: page.locator('svg.lucide-inline'),
    }).first()
    const button = await trigger.boundingBox().catch(() => null)
    const glyph = await trigger.locator('svg.lucide-inline').first().boundingBox().catch(() => null)
    if (!button || !glyph) return null
    const buttonCentre = button.y + button.height / 2
    const glyphCentre = glyph.y + glyph.height / 2
    const measured = {
      buttonCentre,
      glyphCentre,
      offset: Number((glyphCentre - buttonCentre).toFixed(2)),
      centred: Math.abs(glyphCentre - buttonCentre) <= CENTRE_MAX,
      gap: null,
      separated: true,
    }
    if (!armed) return measured
    const count = await trigger.locator('span.font-mono').first().boundingBox().catch(() => null)
    if (!count) return null
    measured.gap = Number((count.x - (glyph.x + glyph.width)).toFixed(2))
    measured.separated = measured.gap > 0
    return measured
  }

  /** Crop the control row itself: the trigger is 32px in a 1500px viewport. */
  async function shot(name, m) {
    const row = await page.getByTestId('composer-control-row').boundingBox()
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: row.x - 6, y: row.y - 10, width: Math.min(360, row.width + 12), height: row.height + 20 },
    })
    console.log('wrote', `${OUT}/${name}.png`, JSON.stringify(m))
  }

  const results = []

  async function scenario(name, armed) {
    await load(armed)
    const m = await measure(armed)
    if (!m) {
      results.push({ name, ok: false, why: 'trigger did not render (button, glyph, or count missing)' })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      return
    }
    await shot(name, m)
    results.push({ name, ok: m.centred && m.separated, ...m })
  }

  await scenario('armed', true)
  await scenario('idle', false)

  await page.close()
  await browser.close()
  srv.close()

  let failed = false
  for (const r of results) {
    if (!r.ok) failed = true
    console.log(r.ok ? 'PASS' : 'FAIL', JSON.stringify(r))
  }
  if (failed) {
    console.error(`\nthe trigger's glyph is not centred within ${CENTRE_MAX}px, or the count is not separated from it`)
    process.exitCode = 1
  }
}

main().catch(err => {
  console.error(err)
  process.exitCode = 1
})
