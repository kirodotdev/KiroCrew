/**
 * Screenshot harness + assertions for CLEARING a stopped automation record.
 *
 * A stopped monitor's record is retained as evidence and REFUSES a re-arm, so
 * the session that stopped watching one pull request cannot watch another until
 * that record is gone. Nothing removed it: the session-automation panel's
 * terminal state offered only Restart (same subject), and the legacy goal
 * popover's button silently no-opped on a structured record.
 *
 * Four frames, two of them controls:
 *
 *   1. stopped monitor -> "Clear stopped monitor" beside Restart, and a line
 *                         naming both exits and the finality of this one.
 *   2. its confirm     -> the erase is irreversible, so one press only asks.
 *   3. live monitor    -> no clear control at all. Clearing a running watch
 *                         would delete it with no record it existed, which the
 *                         server refuses; the surface must not offer the press.
 *   4. stopped legacy  -> the legacy goal view's own button reads "Clear stopped
 *                         goal", the status reads "Stopped", and pressing it
 *                         issues the DELETE carrying its intent.
 *
 * This ASSERTS as well as photographs, because a PNG cannot fail. It drives the
 * REAL built SPA (website/dist) behind `serveDist` with every /api/** call
 * answered from fixtures by `stubDashboardApi` -- no gateway, no dashboard auth,
 * no kiro-cli -- and exits non-zero unless each frame renders what the PR
 * claims. Labels are read from the CATALOG, so a key rename breaks the capture
 * loudly instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-goal-clear-record.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/goal-clear-record'
const SLOT = 'chat-loop'
const PROJECT = '/home/user/workspace/uploader'
const LOOP_ID = 'mon-9615'
const PR = 'https://github.com/kirodotdev/KiroCrew/pull/9615'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const gen = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const goal = { ...gen.components.autoNudgePopover, ...manual.components.autoNudgePopover }
const panel = {
  ...gen.components.sessionAutomationPopover,
  ...manual.components.sessionAutomationPopover,
}
const STOP = goal.stop_loop
const CLEAR_GOAL = goal.clear_stopped_goal
const STOPPED = goal.loop_stopped
const STOPPED_HELP = goal.stopped_help
const START = goal.start_loop
const CLEAR_MONITOR = panel.clear_monitor
const CONFIRM_CLEAR = panel.confirm_clear
const RESTART = panel.restart_monitor
const STOP_MONITOR = panel.stop_monitor
const TERMINAL_EXITS = panel.terminal_exits
for (const [name, value] of Object.entries({
  STOP, CLEAR_GOAL, STOPPED, STOPPED_HELP, START,
  CLEAR_MONITOR, CONFIRM_CLEAR, RESTART, STOP_MONITOR, TERMINAL_EXITS,
})) {
  if (!value) throw new Error(`catalog key for ${name} is missing -- renamed?`)
}

const NOW = Math.floor(Date.now() / 1000)
/** Fixed instants so every timestamp the panel prints is byte-identical per run. */
const FIXED_FIRE_TS = Date.UTC(2026, 8, 9, 14, 11, 0) / 1000
const FIXED_STOP_TS = Date.UTC(2026, 8, 9, 14, 38, 0) / 1000

const slots = [{
  key: SLOT,
  title: 'Watch PR 9615 to review-ready',
  running: false,
  last_message: 'Monitor stopped. Nothing is watching the PR now.',
  messages: 6,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: NOW,
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: NOW - 900, content: 'Stop watching 9615 for now.' },
    { role: 'assistant', ts: NOW - 120, content: 'Stopped. The record is kept for inspection.' },
  ],
}

/** One structured monitor, shaped like `GET /api/monitors/slot/{slot}` serves it. */
const monitorRecord = over => ({
  id: LOOP_ID,
  slot_key: SLOT,
  message: 'structured monitor',
  idle_secs: 420,
  max_cycles: 8,
  cycle_count: 3,
  active: true,
  last_fire_ts: FIXED_FIRE_TS,
  next_due_ts: 0,
  stopped_reason: '',
  monitor: {
    version: 1,
    config_generation: 1,
    kind: 'github_pull_request',
    target: PR,
    objective: 'review_ready',
    cadence_secs: 420,
    wake_instructions: 'Fix legitimate CI failures and review findings, then push. Never merge.',
    budgets: {
      max_runtime_secs: 14_400,
      max_agent_turns: 8,
      max_tokens: 250_000,
      max_provider_errors: 3,
    },
    last_observation: null,
    last_observation_status: 'pending',
    last_observation_reason_code: 'checks_pending',
    last_observed_at: FIXED_FIRE_TS,
    last_fingerprint: 'abc',
    last_wake_fingerprint: '',
    wake_in_flight: false,
    wake_delivery: null,
    wake_count: 2,
    completion_evidence_deadline: 0,
    last_completion_fingerprint: '',
    last_completion_disposition: null,
    last_completed_at: FIXED_FIRE_TS,
    token_usage_known: true,
    agent_turns: 2,
    input_tokens: 1200,
    output_tokens: 300,
    probe_count: 8,
    provider_error_count: 0,
    consecutive_provider_errors: 0,
    last_probe_at: FIXED_FIRE_TS,
    last_decision: 'no_change',
    last_provider_error: null,
    next_probe_at: 0,
    outcome: null,
    stopped_reason: '',
    stopped_at: 0,
    ...over,
  },
  ...(over.outcome ? { active: false } : {}),
})

const stoppedMonitor = monitorRecord({
  outcome: 'user_stop',
  stopped_reason: 'user_stop',
  stopped_at: FIXED_STOP_TS,
  next_probe_at: 0,
})
const liveMonitor = monitorRecord({})

/** One legacy goal loop, as `GET /api/autonudge/slot/{slot}` serves it. */
const legacyLoop = over => ({
  id: LOOP_ID,
  slot_key: SLOT,
  message: `Check ${PR} for new CI results and review comments.`,
  idle_secs: 420,
  max_cycles: 8,
  cycle_count: 3,
  active: true,
  last_fire_ts: FIXED_FIRE_TS,
  next_due_ts: 0,
  stopped_reason: '',
  banner: '',
  stop_sentinel_path: '',
  max_runtime_secs: 14_400,
  ...over,
})

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  // The action row is 12px type; 1x renders the button labels soft enough on
  // GitHub that a reviewer cannot read them.
  deviceScaleFactor: 2,
})

/**
 * Boot the chat page with `legacy` and `structured` as this slot's automation,
 * recording every write the panel sends.
 */
async function load({ legacy = null, structured = null }) {
  const page = await context.newPage()
  logPageProblems(page)
  const writes = []

  const extra = async (path, route) => {
    const request = route.request()
    if (path === `/api/monitors/${LOOP_ID}/clear`) {
      writes.push(`POST ${path}`)
      await json(route, { ok: true, monitor: null })
      return true
    }
    if (path === `/api/autonudge/${LOOP_ID}` && request.method() === 'DELETE') {
      // The full URL, so the frame proves the pressed INTENT travelled with it.
      writes.push(`DELETE ${request.url().replace(base, '')}`)
      await json(route, { ok: true })
      return true
    }
    if (path === `/api/autonudge/slot/${SLOT}`) { await json(route, { loop: legacy }); return true }
    if (path === `/api/monitors/slot/${SLOT}`) {
      await json(route, { enabled: true, monitor: structured })
      return true
    }
    if (path === '/api/autonudge') {
      await json(route, { enabled: true, loops: legacy ? [legacy] : [] })
      return true
    }
    if (path === '/api/monitors') {
      await json(route, { enabled: true, monitors: structured ? [structured] : [] })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, {
    slots,
    extra,
    // Pin the locale: without it the SPA negotiates one from the environment and
    // the frame comes out in whatever language the runner happens to pick.
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  return { page, writes }
}

/** Open the automation popover from the composer chip and return it. */
async function openPopover(page) {
  // Addressed by accessible name, which differs by what is armed: an armed
  // monitor reads "Monitor status: <status>", an active legacy loop reads "Goal
  // active (cycle N)", and nothing armed reads "Set up a bounded monitor".
  const chip = page
    .getByRole('button', { name: /^(Monitor status: |Set up a bounded monitor|Goal active \(cycle |Goal loop armed \(cycle )/ })
    .first()
  await chip.waitFor({ state: 'visible', timeout: 15000 })
  await chip.click()
  const popover = page.getByRole('dialog').first()
  await popover.waitFor({ state: 'visible', timeout: 10000 })
  // Radix plays a zoom/fade entry animation; shoot after it settles.
  await page.waitForTimeout(700)
  return popover
}

const results = []
const check = (name, ok, detailText) => {
  results.push({ name, ok, detail: detailText })
  if (!ok) console.error(`FAIL ${name}: ${detailText}`)
}

async function shoot(popover, name) {
  const out = join(OUT, name)
  await popover.screenshot({ path: out })
  console.log('wrote', out)
}

const has = async (popover, name) => (await popover.getByRole('button', { name }).count())

// 1 + 2 -- a stopped monitor: the clear is offered beside Restart, and asks first.
{
  const { page, writes } = await load({ structured: stoppedMonitor })
  const popover = await openPopover(page)
  await shoot(popover, '01-stopped-monitor-clear-offered.png')
  check('01 offers the clear', (await has(popover, CLEAR_MONITOR)) === 1,
    `"${CLEAR_MONITOR}" not found on a stopped monitor`)
  check('01 still offers Restart', (await has(popover, RESTART)) === 1,
    `"${RESTART}" missing -- clearing must be an ADDITIONAL exit, not a replacement`)
  const exits = await popover.getByTestId('monitor-terminal-exits').innerText()
  check('01 both exits are named', exits.trim() === TERMINAL_EXITS,
    `the exits line reads ${JSON.stringify(exits)}`)

  await popover.getByRole('button', { name: CLEAR_MONITOR }).click()
  await page.waitForTimeout(400)
  await shoot(popover, '02-stopped-monitor-clear-confirm.png')
  check('02 one press only asks', writes.length === 0,
    `the first press already sent ${JSON.stringify(writes)}`)
  check('02 the confirm is offered', (await has(popover, CONFIRM_CLEAR)) === 1,
    `"${CONFIRM_CLEAR}" not found after pressing the clear`)
  await popover.getByRole('button', { name: CONFIRM_CLEAR }).click()
  await page.waitForTimeout(900)
  check('02 the confirm issues the clear',
    writes.length === 1 && writes[0] === `POST /api/monitors/${LOOP_ID}/clear`,
    `the confirm sent ${JSON.stringify(writes)}`)
  await page.close()
}

// 3 -- a LIVE monitor: the control frame. No clear anywhere.
{
  const { page } = await load({ structured: liveMonitor })
  const popover = await openPopover(page)
  await shoot(popover, '03-live-monitor-no-clear.png')
  check('03 no clear on a live monitor', (await has(popover, CLEAR_MONITOR)) === 0,
    `"${CLEAR_MONITOR}" offered while the monitor is still running`)
  check('03 no exits line on a live monitor',
    (await popover.getByTestId('monitor-terminal-exits').count()) === 0,
    'the terminal exits line renders on a live monitor')
  check('03 stopping is still offered', (await has(popover, STOP_MONITOR)) === 1,
    `"${STOP_MONITOR}" missing -- the frame did not render a live monitor at all`)
  await page.close()
}

// 4 -- a stopped LEGACY goal loop: the goal view's own button and status.
{
  const { page, writes } = await load({
    legacy: legacyLoop({ active: false, stopped_reason: 'manual' }),
  })
  const popover = await openPopover(page)
  await shoot(popover, '04-stopped-legacy-goal-clear.png')
  check('04 reads Clear stopped goal', (await has(popover, CLEAR_GOAL)) === 1,
    `"${CLEAR_GOAL}" not found on a stopped legacy loop`)
  check('04 does not read Stop loop', (await has(popover, STOP)) === 0,
    `"${STOP}" still offered on a loop that is already stopped`)
  check('04 offers the way back', (await has(popover, START)) === 1,
    `"${START}" missing -- a stopped loop must still show how to resume`)
  const status = await popover.getByTestId('auto-nudge-loop-paused').innerText()
  check('04 the status reads Stopped', status.trim() === STOPPED,
    `the status line reads ${JSON.stringify(status)}, want ${JSON.stringify(STOPPED)}`)
  const help = await popover.getByTestId('auto-nudge-stopped-help').innerText()
  check('04 both exits are named', help.trim() === STOPPED_HELP,
    `the help line reads ${JSON.stringify(help)}`)

  await popover.getByRole('button', { name: CLEAR_GOAL }).click()
  await page.waitForTimeout(900)
  check('04 the press carries its intent',
    writes.length === 1 && writes[0] === `DELETE /api/autonudge/${LOOP_ID}?intent=clear`,
    `the press sent ${JSON.stringify(writes)}`)
  await page.close()
}

await browser.close()
srv.close()

console.log('--- assertions (each frame must render what the PR claims) ---')
for (const r of results) console.log(JSON.stringify(r))

if (!results.every(r => r.ok)) {
  console.error('FAIL: a frame did not render the clear affordance -- fix the fixture, do not commit the PNG')
  process.exit(1)
}
console.log('OK')
