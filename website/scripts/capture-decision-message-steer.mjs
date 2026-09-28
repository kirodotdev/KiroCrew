/**
 * Screenshot harness for the two surfaces `message.steer` adds.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures. No gateway, no agent and no Jev call: only the network is stubbed, so
 * `readSteerRecord`, the split button's two fail-closed gates, the transcript
 * virtualizer and the user rows render exactly as in production.
 *
 * Two surfaces, because the feature is a choice and its receipt:
 *
 *  - the MODE. `Auto (Jev)` sits beside Steer and Queue in the split send
 *    button's picker, and it is drawn only when the dashboard config reports
 *    `decisions_enabled: true` AND the keystone consent reports `permits: true`.
 *    The `withheld` pass serves the SAME fixtures with the fleet answer flipped
 *    to false and ASSERTS the row is absent, because "the entry appears" is only
 *    half the claim.
 *  - the LINE. One line on the USER row the decision was about -- which path was
 *    taken, how sure Jev was, how long it took. Both answers are photographed,
 *    because "queued" and "steered" are the whole vocabulary and a reader has to
 *    be able to tell them apart at a glance.
 *
 * Usage: node scripts/capture-decision-message-steer.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decision-message-steer'
const SLOT = 'chat-message-steer'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const LINE = '[data-testid="steer-decision-line"]'
const QUEUED = `${LINE}[data-choice="queue"]`
const STEERED = `${LINE}[data-choice="steer"]`

/** Consent is on and pointed at the address it was given for. */
const CONSENT = {
  enabled: true,
  endpoint: 'https://api.typesafe.ai/v1/systemone',
  configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
  permits: true,
}

/** A separate later request: the work in progress finishes first. */
const QUEUE_RECORD = {
  turn_id: 'turn-91c3ab',
  ts: '2026-09-20T07:04:11Z',
  point: 'message.steer',
  choice: 'queue',
  baseline: 'steer',
  p: 0.83,
  latency_ms: 190,
}

/** A correction to what the agent is doing right now: interrupt it. */
const STEER_RECORD = {
  ...QUEUE_RECORD,
  turn_id: 'turn-4d07e2',
  choice: 'steer',
  p: 0.91,
  latency_ms: 150,
}

/**
 * The receipt on a REQUEUED steer's row.
 *
 * Its own `turn_id` and numbers rather than a reuse of the two above, because this
 * row is a different send: the turn ended while the steer RPC was suspended, so the
 * teardown degraded the steer into a queue card and the DRAIN wrote the row.
 */
const REQUEUED_RECORD = {
  turn_id: 'turn-7b21ce',
  ts: '2026-09-20T07:06:02Z',
  point: 'message.steer',
  choice: 'queue',
  baseline: 'steer',
  p: 0.77,
  latency_ms: 210,
}

const t0 = Date.now() / 1000 - 600

const slots = [
  {
    key: SLOT,
    title: 'Rewrite the parser',
    running: true,
    last_message: 'Editing parser.py now.',
    messages: 5,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

const detail = {
  // Running, so the composer offers the split send button this feature extends.
  running: true,
  has_more: false,
  total: 5,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Rewrite the config parser to accept TOML.' },
    {
      role: 'assistant',
      ts: t0 + 12,
      content: 'Editing parser.py now — the TOML reader is in, the schema check is next.',
    },
    {
      role: 'user',
      ts: t0 + 30,
      content: 'And afterwards, bump the version in pyproject.toml.',
      meta: { decisions_strip: QUEUE_RECORD },
    },
    {
      role: 'user',
      ts: t0 + 44,
      content: 'Stop — you are editing the wrong file, the parser lives in config/read.py.',
      meta: { decisions_strip: STEER_RECORD, steer: true, steerState: 'consumed' },
    },
    { role: 'assistant', ts: t0 + 58, content: 'Switched to config/read.py.' },
    {
      // A REQUEUED steer: the turn ended while the steer RPC was suspended, so its
      // teardown moved the message into the queue and the drain wrote this row.
      // The receipt has to be here -- the decision chose this path, and until the
      // side map carried it across that teardown the row read as undecided.
      role: 'user',
      ts: t0 + 72,
      content: 'Also drop the legacy INI branch while you are in there.',
      meta: { decisions_strip: REQUEUED_RECORD, steer: true, steerState: 'requeued' },
    },
    { role: 'assistant', ts: t0 + 96, content: 'Dropped the INI branch too.' },
  ],
}

const failures = []
const check = (ok, msg) => {
  if (!ok) failures.push(msg)
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`)
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
  })

  // The fleet answer and the keystone. Registered AFTER the harness's catch-all so
  // they win: Playwright matches route handlers in reverse order.
  let decisionsEnabled = true
  await page.route('**/api/decisions/consent', route => json(route, CONSENT))
  await page.route('**/api/dashboard/config', route =>
    json(route, {
      restore_sessions: false,
      restore_window_minutes: 30,
      merge_queued_messages: false,
      widget_density: 'more',
      decisions_enabled: decisionsEnabled,
    }),
  )

  /**
   * Pin the locale to English. The harness's init script CLEARS localStorage on
   * every navigation, so the key is written by an init script registered AFTER the
   * harness's first navigation, and a reload is what makes it stick.
   */
  let localePinned = false
  async function loadInEnglish(theme) {
    await load(theme, { selector: LINE })
    if (!localePinned) {
      await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
      localePinned = true
    }
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector(LINE, { timeout: 20000 })
    await page.waitForTimeout(800)
  }

  /** Open the split button's picker with a draft in the composer. */
  async function openPicker() {
    const input = page.getByLabel('Message input').first()
    await input.waitFor({ timeout: 20000 })
    await input.fill('one more thing, while you work')
    await page.waitForTimeout(400)
    const caret = page.getByTestId('busy-send-caret')
    await caret.waitFor({ timeout: 15000 })
    await caret.click()
    await page.getByRole('menu').waitFor({ timeout: 10000 })
    await page.waitForTimeout(400)
  }

  const menuBox = () =>
    page.getByRole('menu').evaluate(el => {
      const r = el.getBoundingClientRect()
      return { x: r.x - 4, y: r.y - 4, width: r.width + 8, height: r.height + 8 }
    })

  for (const theme of ['light', 'dark']) {
    await loadInEnglish(theme)

    // The QUEUE answer on the message it was about: a separate later request, so
    // the work in progress was allowed to finish.
    const queued = page.locator(QUEUED).first()
    await queued.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    // The row, not the line alone: the claim is that the receipt sits on the
    // message it describes, which a clip of the line by itself cannot show.
    const queuedRow = queued.locator('xpath=ancestor::div[@data-role="user"][1]')
    await queuedRow.screenshot({ path: `${OUT}/01-queued-${theme}.png` })
    console.log('wrote', `${OUT}/01-queued-${theme}.png`)
    const queuedText = (await queued.textContent()) ?? ''
    check(
      /after this turn/.test(queuedText)
        && /confidence 0\.83/.test(queuedText)
        && /190/.test(queuedText),
      `${theme}: the queued line names the choice, the labelled score and the latency (${queuedText.trim().slice(0, 80)})`,
    )

    // The STEER answer, on a row the confirmed-steer badge also claims: the badge
    // says WHAT happened and the line says who chose it.
    const steered = page.locator(STEERED).first()
    await steered.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    const steeredRow = steered.locator('xpath=ancestor::div[@data-role="user"][1]')
    await steeredRow.screenshot({ path: `${OUT}/02-steered-${theme}.png` })
    console.log('wrote', `${OUT}/02-steered-${theme}.png`)
    const steeredText = (await steered.textContent()) ?? ''
    check(
      /interrupt/.test(steeredText) && !/after this turn/.test(steeredText),
      `${theme}: the two answers are told apart in words, not only by a glyph (${steeredText.trim().slice(0, 80)})`,
    )
    check(
      !/steered/.test(steeredText),
      `${theme}: the line describes the CHOICE, so it cannot contradict the delivery`,
    )

    // A REQUEUED steer's row still carries its receipt. The row the drain wrote,
    // not the one the steer path persisted -- the receipt reaches it only because
    // the requeue carries it onto the queue entry the drain unions onto this row.
    const requeuedRow = page
      .locator('div[data-role="user"]')
      .filter({ hasText: 'legacy INI branch' })
      .first()
    await requeuedRow.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await requeuedRow.screenshot({ path: `${OUT}/06-requeued-receipt-${theme}.png` })
    console.log('wrote', `${OUT}/06-requeued-receipt-${theme}.png`)
    check(
      (await requeuedRow.locator(LINE).count()) === 1,
      `${theme}: a requeued steer's row draws its one receipt`,
    )

    // The picker, with the third mode offered.
    await openPicker()
    await page.screenshot({ path: `${OUT}/03-picker-${theme}.png`, clip: await menuBox() })
    console.log('wrote', `${OUT}/03-picker-${theme}.png`)
    check(
      await page.getByTestId('busy-send-mode-auto').isVisible(),
      `${theme}: Auto (Jev) is offered beside Steer and Queue`,
    )
    check(
      (await page.getByTestId('busy-send-mode-steer').isVisible())
        && (await page.getByTestId('busy-send-mode-queue').isVisible()),
      `${theme}: both manual modes are still offered`,
    )

    // Selected: the fire half carries the mode, so the composer says what Enter
    // will do rather than leaving it to the menu.
    await page.getByTestId('busy-send-mode-auto').click()
    await page.waitForTimeout(400)
    const composer = page.getByTestId('busy-send-button').locator('xpath=ancestor::div[3]')
    await composer.screenshot({ path: `${OUT}/04-selected-${theme}.png` })
    console.log('wrote', `${OUT}/04-selected-${theme}.png`)
    check(
      (await page.getByTestId('busy-send-button').getAttribute('data-mode')) === 'auto',
      `${theme}: the fire half reports the selected mode`,
    )
  }

  // ── The withheld pass: the fleet says no, so the mode is not offered ──
  decisionsEnabled = false
  await loadInEnglish('dark')
  await openPicker()
  check(
    (await page.getByTestId('busy-send-mode-auto').count()) === 0,
    'withheld: Auto (Jev) is absent when decisions_enabled is false',
  )
  check(
    await page.getByTestId('busy-send-mode-steer').isVisible(),
    'withheld: the two manual modes are untouched',
  )
  await page.screenshot({ path: `${OUT}/05-picker-withheld.png`, clip: await menuBox() })
  console.log('wrote', `${OUT}/05-picker-withheld.png`)
  // A stored `auto` must not survive the withdrawal: the fire half falls back to
  // the shipped default rather than sending a flag the gateway would not decide.
  check(
    (await page.getByTestId('busy-send-button').getAttribute('data-mode')) === 'steer',
    'withheld: a session that had picked Auto falls back to Steer',
  )

  // The lines are UNAFFECTED by the fleet answer: a stamped record is history that
  // already sits on this machine, so drawing it sends nothing.
  check(
    (await page.locator(LINE).count()) >= 2,
    'withheld: the receipts for past decided sends are still drawn',
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
