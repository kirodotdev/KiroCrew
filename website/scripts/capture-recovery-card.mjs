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
import { mkdirSync, writeFileSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/recovery-card'
const FRAMES = process.argv[3] || ''
const SLOT = 'chat-recovery'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })
if (FRAMES) mkdirSync(FRAMES, { recursive: true })

/** The card is what the shots are of, so every load waits for one to mount. */
const RECOVERY_WAIT = { selector: '[data-testid="recovery-card"]' }

const REFUSAL = '[Tool refusal — automatic recovery]'
const STALLED = '[Stalled turn — automatic recovery]'
const TOOL_STALL = '[Tool stall — automatic recovery]'
const HOOK = '[Hook continuation — automatic]'

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
    { role: 'inject', ts: t0 + 615, content: `${HOOK}\nYour previous turn ended by offering a choice where one option is a trivial read-only operation. Do not wait for the user: perform the trivial read-only option now, then continue with your work. If the result surfaces a new choice, this hook may fire again — that is intentional (loop until the read path is exhausted).`, meta: {} },
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

  /**
   * Element-scoped shots of ONE card kind, collapsed then expanded.
   *
   * The full-viewport shots above frame every kind together, which is what
   * proves prefix detection; these crop to a single card so its own copy is
   * legible — the point of interest for a kind whose labels differ from the
   * rest of the family. Toggled back afterwards so the following viewport shot
   * still captures the state it expects.
   */
  async function hookShots(theme) {
    const card = page.locator('[data-testid="recovery-card"][data-kind="hook"]').first()
    if (!(await card.count())) {
      throw new Error('no hook recovery card rendered — fixture or PREFIXES drift')
    }
    const toggle = card.getByTestId('recovery-card-toggle').first()
    await card.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await card.screenshot({ path: `${OUT}/hook-collapsed-${theme}.png` })
    console.log('wrote', `${OUT}/hook-collapsed-${theme}.png`)

    await toggle.evaluate(el => el.click())
    await page.waitForTimeout(500)
    await card.screenshot({ path: `${OUT}/hook-expanded-${theme}.png` })
    console.log('wrote', `${OUT}/hook-expanded-${theme}.png`)

    await toggle.evaluate(el => el.click())
    await page.waitForTimeout(300)
  }

  /**
   * Frame sequence of ONE expand/collapse cycle, for assembling an animated GIF.
   *
   * Frames are ELEMENT-scoped, so each is exactly the card and nothing else. A
   * page-level clip sized for the expanded card would leave the collapsed frames
   * showing whatever sits below it — the next message, cropped mid-word. The
   * frames therefore vary in height; the assembler pastes them onto one canvas
   * filled with the page background colour recorded in `bg.txt`.
   *
   * Only runs when a frames dir is passed as argv[3].
   */
  async function hookFrames(theme, framesDir) {
    const card = page.locator('[data-testid="recovery-card"][data-kind="hook"]').first()
    const toggle = card.getByTestId('recovery-card-toggle').first()

    await card.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }))
    await page.waitForTimeout(400)

    const bg = await page.evaluate(() => {
      const walk = el => {
        while (el) {
          const c = getComputedStyle(el).backgroundColor
          if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c
          el = el.parentElement
        }
        return getComputedStyle(document.body).backgroundColor
      }
      return walk(document.querySelector('[data-testid="recovery-card"]').parentElement)
    })
    writeFileSync(`${framesDir}/bg.txt`, bg)

    let n = 0
    const frame = async () => {
      await card.screenshot({ path: `${framesDir}/${theme}-${String(n).padStart(3, '0')}.png` })
      n += 1
    }

    for (let i = 0; i < 3; i += 1) await frame()          // hold, collapsed
    await toggle.evaluate(el => el.click())
    for (let i = 0; i < 6; i += 1) { await page.waitForTimeout(40); await frame() }
    await page.waitForTimeout(250)
    for (let i = 0; i < 4; i += 1) await frame()          // hold, expanded
    await toggle.evaluate(el => el.click())
    for (let i = 0; i < 6; i += 1) { await page.waitForTimeout(40); await frame() }
    console.log(`wrote ${n} frames to ${framesDir} (bg ${bg})`)
  }

  await load('dark', RECOVERY_WAIT)
  const cards = page.getByTestId('recovery-card')
  console.log('cards rendered:', await cards.count())
  console.log('kinds:', await cards.evaluateAll(els => els.map(e => e.dataset.kind)))
  console.log('titles:', await cards.evaluateAll(els => els.map(e => e.innerText.replace(/\n/g, ' | ').slice(0, 90))))
  await expandTurn()
  await shot('collapsed-dark')
  await hookShots('dark')
  if (FRAMES) await hookFrames('dark', FRAMES)

  await toggleFirstCard()
  await shot('expanded-dark')

  await load('light', RECOVERY_WAIT)
  await expandTurn()
  await shot('collapsed-light')
  await hookShots('light')
  await toggleFirstCard()
  await shot('expanded-light')

  await close()
}

main().catch(err => { console.error(err); process.exit(1) })
