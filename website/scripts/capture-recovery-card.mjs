/**
 * Screenshot harness for the in-transcript recovery card.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call and the /api/ws websocket answered from fixtures. No gateway, no
 * token, no agent. The client code is unmodified — only the network is stubbed —
 * so the transcript, its virtualizer and the card render exactly as in
 * production.
 *
 * The fixture transcript carries one row of each recovery kind (tool refusal,
 * stalled turn, stalled tool) using the verbatim prefixes the gateway prepends,
 * so the shots prove the prefix detection as well as the layout.
 *
 * Usage: node scripts/capture-recovery-card.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/recovery-card'
const SLOT = 'chat-recovery'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** The card is what the shots are of, so every load waits for one to mount. */
const RECOVERY_WAIT = { selector: '[data-testid="recovery-card"]' }

const REFUSAL = '[Tool refusal — automatic recovery]'
const STALLED = '[Stalled turn — automatic recovery]'
const TOOL_STALL = '[Tool stall — automatic recovery]'

const refusalBody = [
  REFUSAL,
  'One or more tool calls in your previous turn were blocked by a Kiro Crew safety policy, which ended the turn early. This was NOT a user action — do not treat it as a cancellation or interruption by the user.',
  '',
  'Blocked:',
  '  - Running: echo "== mypy =="; .venv/bin/mypy src/kiro_crew/config/loader.py src/kiro_crew/dashboard/handlers/core.py 2>&1 | tail -15; echo "== regenerate baseline =="; .venv/bin/python scripts/generate_config...: Blocked by security policy: .*env.*grep.*AWS.*',
  '',
  'Decide how to proceed: use an allowed alternative (for a shell command, a read-only variant), a different tool, or — if the block is correct and you genuinely cannot proceed — say so and stop. Otherwise continue the task where you left off.',
].join('\n')

const t0 = Date.now() / 1000 - 900
const slots = [{
  key: SLOT,
  title: 'Verify the config loader change',
  running: false,
  last_message: 'Continuing the remaining verification.',
  messages: 7,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 7,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Run the backend gates on the changed modules and regenerate the config baseline.' },
    { role: 'assistant', ts: t0 + 10, content: 'Running mypy on the changed modules, then regenerating the schema baseline.' },
    { role: 'inject', ts: t0 + 40, content: refusalBody, meta: {} },
    { role: 'assistant', ts: t0 + 55, content: 'That block was a false positive from a safety pattern — my command happened to combine `.venv`, `grep`, and an aws string in one line. Re-running without the pipe.' },
    { role: 'inject', ts: t0 + 300, content: `${STALLED}\nYour previous turn was interrupted by a system stall and has been automatically recovered. This was NOT a user action — do not treat it as a cancellation or interruption by the user. The work you already completed is preserved in the conversation above. Continue from where you left off and finish the task; do not restart it or repeat steps that already succeeded.`, meta: {} },
    { role: 'inject', ts: t0 + 600, content: `${TOOL_STALL}\nA tool call in your previous turn stopped producing output and was cancelled by the session watchdog. This was NOT a user action. The command redirected its output to build.log — inspect the tail of that file to see how far it got before re-running anything.`, meta: {} },
    { role: 'assistant', ts: t0 + 620, content: 'Gates are green: mypy clean on both modules and the baseline regenerated with no diff.' },
  ],
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
  })

  /**
   * Expand the turn's reasoning pane.
   *
   * `inject` rows are not in TurnBlock's always-visible set, so with
   * collapse-reasoning on they sit inside the "Worked through N steps" pane —
   * exactly where they sit today. Open it so the shots frame the card in the
   * position a user reads it.
   */
  async function expandTurn() {
    const toggle = page.getByRole('button', { name: /Worked through \d+ steps/ })
    if (await toggle.count()) {
      await toggle.first().evaluate(el => el.click())
      await page.waitForTimeout(500)
    }
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /**
   * Toggle the first recovery card.
   *
   * The transcript is virtualized: rows are absolutely positioned and a
   * neighbouring row's box can sit over the card, so Playwright's hit-testing
   * click times out. Dispatching the click on the node itself still runs the
   * real React onClick — which is what is under test here — without depending
   * on the virtualizer's stacking.
   */
  async function toggleFirstCard() {
    await page.getByTestId('recovery-card-toggle').first().evaluate(el => {
      el.scrollIntoView({ block: 'center' })
      el.click()
    })
    await page.waitForTimeout(500)
  }

  await load('dark', RECOVERY_WAIT)
  const cards = page.getByTestId('recovery-card')
  console.log('cards rendered:', await cards.count())
  console.log('kinds:', await cards.evaluateAll(els => els.map(e => e.dataset.kind)))
  console.log('titles:', await cards.evaluateAll(els => els.map(e => e.innerText.replace(/\n/g, ' | ').slice(0, 90))))
  await expandTurn()
  await shot('collapsed-dark')

  await toggleFirstCard()
  await shot('expanded-dark')

  await load('light', RECOVERY_WAIT)
  await expandTurn()
  await shot('collapsed-light')
  await toggleFirstCard()
  await shot('expanded-light')

  await close()
}

main().catch(err => { console.error(err); process.exit(1) })
