/**
 * Screenshot harness for how the automation popover renders an UNLIMITED wake
 * budget.
 *
 * `max_agent_turns` reads 0 as unlimited (the contract default is 0), so the
 * number under the "Maximum agent turns" label is not the whole reading: a bare
 * 0 there says the opposite of what it means.
 * Three states carry the surface:
 *
 *  - `unlimited-monitor`: an armed watch with `max_agent_turns: 0`. The budget
 *    row must read the localized word, not `0`.
 *  - `finite-monitor`: the same watch with an explicit 6, proving the word
 *    replaces only the sentinel and a real ceiling still renders as its number.
 *  - `create-form`: the arm form, whose input prefills the contract default and
 *    carries the hint that says what entering 0 means, with the control's
 *    `min` / `max` read from the contract.
 *
 * It ASSERTS as well as photographs, against the REAL built SPA in
 * website/dist: a regression that puts the digit back, or drops the hint, or
 * changes the input's bounds away from the contract, is a non-zero exit rather
 * than a photo nobody compares.
 *
 * Labels are read from the CATALOG, and the numeric bounds from contract.json,
 * so a key rename or a bound change breaks the capture loudly instead of
 * silently screenshotting the wrong element.
 *
 * Nothing in CI runs this file; it is a manual guard and the source of the PR's
 * evidence. The CI-enforced half is src/test/SessionAutomationPopover.test.tsx.
 *
 * Gateway-free: serveDist + stubDashboardApi, so it never dials a live instance
 * and needs no token.
 *
 * Usage: node scripts/capture-monitor-wake-budget-unlimited.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { goalPopoverSlots, goalPopoverDetail } from './lib/goal-popover-fixture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/monitor-wake-budget-unlimited'
const SLOT = 'chat-monitor'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const sp = manual.components.sessionAutomationPopover
const TURNS_LABEL = sp.maximum_agent_turns
const UNLIMITED = sp.unlimited
const ZERO_HINT = sp.wake_budget_zero_hint
if (!TURNS_LABEL || !UNLIMITED || !ZERO_HINT) {
  throw new Error('components.sessionAutomationPopover wake-budget keys missing -- renamed?')
}

const contract = JSON.parse(
  readFileSync(fileURLToPath(new URL('../src/monitoring/contract.json', import.meta.url)), 'utf-8'),
)
const LIMITS = contract.limits.maxAgentTurns

const slots = goalPopoverSlots(SLOT, PROJECT)
const detail = goalPopoverDetail(PROJECT)

const observation = {
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
}

/* The surface reads the LOOP record, with the monitor nested inside it, which is
   the shape `/api/monitors/slot/` returns for an armed watch. Handing it the bare
   monitor leaves the popover reading "nothing armed" and opening the goal
   editor -- which photographs the wrong panel rather than failing. */
const loopFor = maxAgentTurns => ({
  id: 'loop-1',
  slot_key: SLOT,
  message: 'Address actionable review feedback.',
  idle_secs: 300,
  max_cycles: maxAgentTurns,
  cycle_count: 12,
  active: true,
  last_fire_ts: 1_800_000_000,
  next_due_ts: 1_800_000_300,
  stopped_reason: '',
  monitor: monitorFor(maxAgentTurns),
})

const monitorFor = maxAgentTurns => ({
  version: 1,
  config_generation: 1,
  kind: 'github_pull_request',
  target: 'https://github.com/kirodotdev/KiroCrew/pull/42',
  objective: 'review_ready',
  cadence_secs: 300,
  wake_instructions: 'Address actionable review feedback.',
  budgets: {
    max_runtime_secs: 14_400,
    max_agent_turns: maxAgentTurns,
    max_tokens: 250_000,
    max_provider_errors: 3,
  },
  last_observation: observation,
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
  agent_turns: 12,
  input_tokens: 1200,
  output_tokens: 300,
  probe_count: 5,
  provider_error_count: 0,
  consecutive_provider_errors: 0,
  last_probe_at: 1_800_000_000,
  last_decision: 'no_change',
  last_provider_error: null,
  next_probe_at: 1_800_000_300,
  outcome: null,
  stopped_reason: '',
  stopped_at: 0,
})

/**
 * `monitor` is null for the arm form and a record for an armed watch. Each
 * branch awaits `json()` and returns true, because the shared stub treats a
 * falsy return as "not handled" and fulfils the route itself -- returning
 * `json(...)` alone double-fulfils.
 */
const extraFor = monitor => async (path, route) => {
  if (path === `/api/autonudge/slot/${SLOT}`) {
    await json(route, { enabled: true, loop: null })
    return true
  }
  if (path === `/api/monitors/slot/${SLOT}`) {
    await json(route, { enabled: true, monitor, max_runtime_ceiling_secs: 604_800 })
    return true
  }
  if (path === '/api/monitors') {
    await json(route, { enabled: true, monitors: [] })
    return true
  }
  if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1000 },
    // The panel is 11-12px type; 1x renders it soft on GitHub.
    deviceScaleFactor: 2,
    /* Every assertion addresses a control by its ENGLISH accessible name, so
       the render has to be English for those lookups to mean anything. Two
       pins, because the language is decided twice: this fixes what the browser
       reports, and `mc-lang` below fixes what the app resolves from it. */
    locale: 'en-US',
  })

  const results = []

  async function shoot(name, { monitor, theme, check }) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme,
      extra: extraFor(monitor),
      localStorageEntries: { 'mc-lang': 'en' },
    })
    await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    /* An armed watch replaces the named composer button with an icon trigger that
       carries no accessible name, so the two states are addressed differently:
       by name when unarmed, and by its glyph inside the composer control row
       when armed -- the same locator capture-monitor-trigger-centering.mjs uses. */
    const trigger = monitor
      ? page.locator('[data-testid="composer-control-row"] button')
        .filter({ has: page.locator('svg.lucide-inline') }).first()
      : page.getByRole('button', { name: 'Set a goal' }).first()
    if (!(await trigger.count().then(n => n > 0).catch(() => false))) {
      results.push({ name, ok: false, why: 'no automation trigger on the composer' })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      await page.close()
      return
    }
    await trigger.click()
    await page.waitForTimeout(700)

    // The arm form opens on the goal editor; switch to the pull-request surface.
    if (!monitor) {
      const offer = page.getByRole('button', { name: 'Watch a pull request instead' }).first()
      if (await offer.count().then(n => n > 0).catch(() => false)) {
        await offer.click()
        await page.waitForTimeout(600)
      }
    }

    const panel = page.locator('[data-side]').first()
    const box = await panel.boundingBox().catch(() => null)
    if (!box) {
      results.push({ name, ok: false, why: 'popover panel did not render' })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      await page.close()
      return
    }

    const verdict = await check(page)

    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x: Math.max(0, box.x - 12),
        y: Math.max(0, box.y - 12),
        width: box.width + 24,
        height: box.height + 24,
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
    results.push({ name, ...verdict })
    await page.close()
  }

  const seesText = (page, text) =>
    page.getByText(text, { exact: true }).first().count().then(n => n > 0).catch(() => false)

  // An armed watch with no wake ceiling must read the word, never the digit.
  const unlimitedCheck = async page => {
    const word = await seesText(page, UNLIMITED)
    const label = await seesText(page, TURNS_LABEL)
    return { ok: word && label, showsUnlimitedWord: word, showsTurnsLabel: label }
  }

  // A real ceiling still renders as its number, and the word is absent.
  const finiteCheck = async page => {
    const six = await seesText(page, '6')
    const word = await seesText(page, UNLIMITED)
    return { ok: six && !word, showsSix: six, showsUnlimitedWord: word }
  }

  // The arm form discloses what entering 0 means, and the control's bounds
  // match contract.json's maxAgentTurns limits.
  const formCheck = async page => {
    const input = page.getByRole('spinbutton', { name: TURNS_LABEL }).first()
    const present = await input.count().then(n => n > 0).catch(() => false)
    const min = present ? await input.getAttribute('min') : null
    const max = present ? await input.getAttribute('max') : null
    const value = present ? await input.inputValue() : null
    const hint = await seesText(page, ZERO_HINT)
    return {
      ok: present
        && hint
        && min === String(LIMITS.minimum)
        && max === String(LIMITS.maximum)
        && value === String(LIMITS.defaultValue),
      showsZeroHint: hint,
      min,
      max,
      prefill: value,
    }
  }

  await shoot('01-unlimited-budget-dark', {
    monitor: loopFor(0), theme: 'dark', check: unlimitedCheck,
  })
  await shoot('02-unlimited-budget-light', {
    monitor: loopFor(0), theme: 'light', check: unlimitedCheck,
  })
  await shoot('03-finite-budget-dark', {
    monitor: loopFor(6), theme: 'dark', check: finiteCheck,
  })
  await shoot('04-create-form-dark', {
    monitor: null, theme: 'dark', check: formCheck,
  })

  await browser.close()
  srv.close()

  console.log('--- assertions ---')
  console.log(`contract maxAgentTurns: min=${LIMITS.minimum} max=${LIMITS.maximum} default=${LIMITS.defaultValue}`)
  for (const r of results) console.log(JSON.stringify(r))

  if (results.length !== 4 || !results.every(r => r.ok)) {
    console.error('FAIL: the wake budget did not render as expected')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
