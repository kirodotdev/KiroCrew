/**
 * Screenshot harness for the two surfaces `model.route` adds.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures. No gateway, no agent and no Jev call: only the network is stubbed, so
 * `readDecisionRecords`, the picker's two fail-closed gates, the transcript
 * virtualizer and both rows render exactly as in production.
 *
 * Two surfaces, because the feature is a choice and its receipt:
 *
 *  - the PICKER. `Auto (Jev)` sits beside `auto`, and it is drawn only when the
 *    dashboard config reports `decisions_enabled: true` AND the keystone consent
 *    reports `enabled: true`. The `withheld` pass serves the SAME rows with the
 *    fleet answer flipped to false and ASSERTS the row is absent, because "the
 *    entry appears" is only half the claim.
 *  - the STRIP. A reply decided by BOTH points carries two rows, so the fixture
 *    below is a turn where `skills.select` chose a skill and `model.route` sent
 *    the turn to the complex tier. That is the frame that shows the two receipts
 *    are distinguishable at a glance rather than described as being.
 *
 * Usage: node scripts/capture-decision-model-route.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decision-model-route'
const SLOT = 'chat-model-route'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const MODEL_SEL = '[data-testid="decision-strip-model"]'
const MODEL_WAIT = { selector: MODEL_SEL }

/** Consent is on and pointed at the address it was given for. */
const CONSENT = {
  enabled: true,
  endpoint: 'https://api.typesafe.ai/v1/systemone',
  configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
  permits: true,
}

/** The skill decision on the same turn, so the two rows are seen together. */
const SKILLS = {
  turn_id: 'turn-4f2a9c',
  ts: '2026-09-20T07:04:11Z',
  point: 'skills.select',
  baseline: ['brazil', 'crux-code-reviews'],
  jev: ['brazil'],
  p: 0.81,
  tokens_saved: 3240,
  candidates: 42,
  batches: 1,
  history_chars: 0,
  truncated: 0,
  dropped: [],
  error: null,
}

/** The turn was judged complex and answered by the complex tier's model. */
const ROUTED = {
  turn_id: 'turn-7b1c40',
  ts: '2026-09-20T07:04:11Z',
  point: 'model.route',
  tier: 'complex',
  model_chosen: 'claude-fable-5.1',
  baseline_model: 'claude-opus-4.8',
  p: 0.91,
  latency_ms: 180,
  history_chars: 0,
  truncated: 0,
  error: null,
}

/**
 * The SHIPPED state: the tier was answered and that tier is unpinned, so the turn
 * kept its session's model. Every tier ships unpinned (no model id may be a
 * hardcoded default), so this is the row most readers see first — and the one that
 * tells them there is something to pin.
 */
const UNPINNED = {
  ...ROUTED,
  turn_id: 'turn-9c2e18',
  tier: 'simple',
  model_chosen: '',
  p: 0.88,
  latency_ms: 140,
}

/**
 * The switch was asked for and did not take. A backend that judges the model VALUE
 * can exhaust its candidate ladder and stay on its own default without failing, so
 * the row records what the turn RAN on rather than what was chosen.
 */
const NOT_APPLIED = {
  ...ROUTED,
  turn_id: 'turn-2d81aa',
  tier: 'medium',
  applied: false,
  model_used: 'claude-opus-4.8',
  p: 0.77,
  latency_ms: 160,
}

/** A pinned id this account cannot run: the turn keeps its model and the row says why. */
const ROUTE_ERROR = {
  ...ROUTED,
  turn_id: 'turn-5ab3f7',
  tier: 'simple',
  error: 'model-not-advertised',
  p: 0.86,
  latency_ms: 150,
}

const t0 = Date.now() / 1000 - 600

const slots = [
  {
    key: SLOT,
    title: 'Redesign the scheduler',
    running: false,
    last_message: 'Here is the shape I would take, and the one trade-off it forces.',
    messages: 4,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    // A routed slot pins nothing: it stores `auto` and carries the flag.
    model: 'auto',
    jev_route: true,
    served_model: 'claude-opus-4.8',
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

const detail = {
  running: false,
  has_more: false,
  total: 8,
  queue: [],
  project: PROJECT,
  model: 'auto',
  jev_route: true,
  messages: [
    { role: 'user', ts: t0, content: 'Rename `flush_batch` to `drain_batch` everywhere.' },
    {
      role: 'assistant',
      ts: t0 + 9,
      content: 'Renamed it in 4 files. Nothing else referenced the old name.',
      meta: { decisions_strip: [UNPINNED] },
    },
    {
      role: 'user',
      ts: t0 + 20,
      content: 'Tighten the retry backoff so a flaky worker cannot starve the queue.',
    },
    {
      role: 'assistant',
      ts: t0 + 31,
      content: 'Backoff is now capped, and the cap is read from config rather than hardcoded.',
      meta: { decisions_strip: [NOT_APPLIED] },
    },
    {
      role: 'user',
      ts: t0 + 40,
      content: 'Add a metric for queue wait time.',
    },
    {
      role: 'assistant',
      ts: t0 + 52,
      content: 'Added it beside the existing depth gauge.',
      meta: { decisions_strip: [ROUTE_ERROR] },
    },
    { role: 'user', ts: t0 + 60, content: 'Now redesign the scheduler so a stalled job cannot hold the queue.' },
    {
      role: 'assistant',
      ts: t0 + 88,
      content: 'Here is the shape I would take, and the one trade-off it forces.',
      meta: { decisions_strip: [SKILLS, ROUTED] },
    },
  ],
}

const failures = []
const check = (ok, msg) => {
  if (!ok) failures.push(msg)
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`)
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({ slot: SLOT, project: PROJECT, slots, detail })

  // The fleet answer and the keystone, both ON. Registered AFTER the harness's
  // catch-all so they win: Playwright matches route handlers in reverse order.
  let decisionsEnabled = true
  await page.route('**/api/decisions/consent', route => json(route, CONSENT))
  await page.route('**/api/dashboard/config', route => json(route, {
    restore_sessions: false,
    restore_window_minutes: 30,
    merge_queued_messages: false,
    widget_density: 'more',
    decisions_enabled: decisionsEnabled,
  }))

  /**
   * Pin the locale to English. The harness's init script CLEARS localStorage on
   * every navigation, so the key is written by an init script registered AFTER
   * the harness's first navigation and a reload is what makes it stick.
   */
  let localePinned = false
  async function loadInEnglish(theme, wait) {
    await load(theme, wait)
    if (!localePinned) {
      await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
      localePinned = true
    }
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector(wait.selector, { timeout: 20000 })
    await page.waitForTimeout(800)
  }

  /**
   * The transcript is virtualized: rows are absolutely positioned and a
   * neighbouring row's box can sit over the strip, so Playwright's hit-testing
   * click times out. Dispatching on the node still runs the real React onClick.
   */
  async function open(selector) {
    await page.locator(`${selector} [data-testid="decision-strip-model-toggle"]`).first().evaluate(el => {
      el.scrollIntoView({ block: 'center' })
      el.click()
    })
    await page.waitForTimeout(500)
  }

  for (const theme of ['light', 'dark']) {
    await loadInEnglish(theme, MODEL_WAIT)

    // Both receipts on one reply: the skill row and the model row together, which
    // is the frame that shows they are distinguishable at a glance.
    const pair = page.locator('[data-testid="decision-strip-model"][data-tier="complex"]').first()
    await pair.evaluate(el => {
      const row = el.parentElement ?? el
      row.scrollIntoView({ block: 'center' })
    })
    await page.waitForTimeout(400)
    const bothBox = await page.locator('[data-testid="decision-strip"]').first().evaluate((skill, modelSel) => {
      const model = document.querySelector(modelSel)
      const a = skill.getBoundingClientRect()
      const b = model.getBoundingClientRect()
      return {
        x: Math.min(a.x, b.x) - 4,
        y: Math.min(a.y, b.y) - 4,
        width: Math.max(a.right, b.right) - Math.min(a.x, b.x) + 8,
        height: Math.max(a.bottom, b.bottom) - Math.min(a.y, b.y) + 8,
      }
    }, MODEL_SEL)
    await page.screenshot({ path: `${OUT}/01-both-receipts-${theme}.png`, clip: bothBox })
    console.log('wrote', `${OUT}/01-both-receipts-${theme}.png`)

    // The shipped unpinned row: answered, reported, nothing applied.
    const unpinned = page.locator('[data-testid="decision-strip-model"][data-pinned="false"]').first()
    await unpinned.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await unpinned.screenshot({ path: `${OUT}/02-collapsed-unpinned-${theme}.png` })
    console.log('wrote', `${OUT}/02-collapsed-unpinned-${theme}.png`)
    const unpinnedText = await unpinned.textContent()
    check(
      /\(unpinned\)/.test(unpinnedText ?? ''),
      `${theme}: an unpinned tier says so instead of leaving a gap (${(unpinnedText ?? '').trim().slice(0, 60)})`,
    )

    // Open: the tier, both models, and what the question carried.
    const COMPLEX_SEL = '[data-testid="decision-strip-model"][data-tier="complex"]'
    await open(COMPLEX_SEL)
    await page.locator(COMPLEX_SEL).first().screenshot({ path: `${OUT}/03-expanded-${theme}.png` })
    console.log('wrote', `${OUT}/03-expanded-${theme}.png`)

    // A switch that did not take: the row names the model the turn RAN on, and the
    // expanded panel's "Model used" agrees with it rather than naming the ask.
    const NOT_APPLIED_SEL = '[data-testid="decision-strip-model"][data-applied="false"]'
    const notApplied = page.locator(NOT_APPLIED_SEL).first()
    await notApplied.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await notApplied.screenshot({ path: `${OUT}/06-not-applied-${theme}.png` })
    console.log('wrote', `${OUT}/06-not-applied-${theme}.png`)
    const notAppliedText = await notApplied.textContent()
    check(
      /claude-opus-4\.8/.test(notAppliedText ?? ''),
      `${theme}: a switch that did not take names the model the turn ran on (${(notAppliedText ?? '').trim().slice(0, 80)})`,
    )
    await open(NOT_APPLIED_SEL)
    await page.locator(NOT_APPLIED_SEL).first().screenshot({
      path: `${OUT}/07-not-applied-expanded-${theme}.png`,
    })
    console.log('wrote', `${OUT}/07-not-applied-expanded-${theme}.png`)
    const expandedNotApplied = await page.locator(NOT_APPLIED_SEL).first().textContent()
    check(
      !/Model used:?\s*claude-fable/.test(expandedNotApplied ?? ''),
      `${theme}: the expanded panel does not credit the model that was not used`,
    )

    // A pinned id this account cannot run: the one routing failure an owner can fix.
    // The error row carries a pin AND the `simple` tier, which nothing else does:
    // the unpinned row shares its tier but not its pin, so this stays unique before
    // the row is expanded -- and the notice only exists once it is.
    const ERROR_SEL =
      '[data-testid="decision-strip-model"][data-tier="simple"][data-pinned="true"]'
    await open(ERROR_SEL)
    const errorRow = page.locator(ERROR_SEL).first()
    await errorRow.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await errorRow.screenshot({ path: `${OUT}/08-error-expanded-${theme}.png` })
    console.log('wrote', `${OUT}/08-error-expanded-${theme}.png`)
    check(
      await errorRow.locator('[data-testid="decision-strip-model-error"]').isVisible(),
      `${theme}: a routing failure is shown on the row rather than only in the log`,
    )

    // ── The picker's `Auto (Jev)` entry ──
    const capsule = page.locator('[title^="Model:"]').first()
    await capsule.waitFor({ timeout: 25000 })
    await capsule.click()
    await page.waitForTimeout(900)
    const dd = page.locator('[role="listbox"][aria-label]').first()
    await dd.waitFor({ timeout: 10000 })
    const popBox = () => dd.evaluate(el => {
      const pop = el.closest('.fixed') ?? el
      const r = pop.getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height }
    })
    await page.screenshot({ path: `${OUT}/04-picker-${theme}.png`, clip: await popBox() })
    console.log('wrote', `${OUT}/04-picker-${theme}.png`)

    // Selected by the row's ID, not its visible text: `auto:jev` renders the
    // label "Auto (Jev)", so a text assertion would be locale-dependent.
    const rows = await page.$$eval('[role="option"]', els =>
      els.map(el => el.querySelector('[data-model-id]')?.getAttribute('data-model-id') ?? ''))
    check(rows[0] === 'auto:jev', `${theme}: the routed entry is the picker's first row (got ${rows.slice(0, 3).join(', ')})`)
    const selected = await page.$$eval('[role="option"][aria-selected="true"] [data-model-id]',
      els => els.map(el => el.getAttribute('data-model-id') ?? ''))
    check(
      selected.length === 1 && selected[0] === 'auto:jev',
      `${theme}: a routed slot reads as selected on that row, not on auto (got ${selected.join(', ') || 'none'})`,
    )
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  }

  // ── The withheld pass: the fleet says no, so the row is not offered ──
  decisionsEnabled = false
  await loadInEnglish('dark', MODEL_WAIT)
  const capsule = page.locator('[title^="Model:"]').first()
  await capsule.waitFor({ timeout: 25000 })
  await capsule.click()
  await page.waitForTimeout(900)
  await page.locator('[role="listbox"][aria-label]').first().waitFor({ timeout: 10000 })
  const withheldRows = await page.$$eval('[role="option"]', els =>
    els.map(el => el.querySelector('[data-model-id]')?.getAttribute('data-model-id') ?? ''))
  check(
    !withheldRows.includes('auto:jev'),
    `withheld: the routed entry is absent when decisions_enabled is false (got ${withheldRows.slice(0, 3).join(', ')})`,
  )
  const dd = page.locator('[role="listbox"][aria-label]').first()
  await page.screenshot({
    path: `${OUT}/05-picker-withheld.png`,
    clip: await dd.evaluate(el => {
      const pop = el.closest('.fixed') ?? el
      const r = pop.getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height }
    }),
  })
  console.log('wrote', `${OUT}/05-picker-withheld.png`)

  // The strip is UNAFFECTED by the fleet answer: a stamped record is history that
  // already sits on this machine, so drawing it sends nothing.
  check(
    (await page.locator(MODEL_SEL).count()) > 0,
    'withheld: the receipt for a past routed turn is still drawn',
  )

  await close()

  if (failures.length) {
    console.error(`\n${failures.length} assertion(s) failed:`)
    for (const f of failures) console.error(`  - ${f}`)
    process.exit(1)
  }
  console.log('\nall assertions passed')
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
