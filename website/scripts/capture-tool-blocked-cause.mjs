/**
 * Screenshot harness for the in-band tool-blocked card's expanded body.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness,
 * with every /api/** call answered from fixtures. No gateway, no token, no
 * agent — only the network is stubbed, so `parseRecoveryMessage`, the
 * transcript virtualizer and the card render exactly as in production.
 *
 * The fixture row is the VERBATIM wire string the gateway writes today:
 * `chat_runner.py` appends `f"{REFUSAL_INBAND_RECOVERY_PREFIX} {cause}\n{notice}"`,
 * so the cause token rides the marker line. Nothing here reformats it — the
 * whole point of the shot is what the card does with that exact input.
 *
 * `invalid_name` is the cause chosen for the shot because it is the one a
 * reader is most likely to misread as their own mistake: it is the host's name
 * for its own validation failure.
 *
 * Usage: node scripts/capture-tool-blocked-cause.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/tool-blocked-cause'
const SLOT = 'chat-tool-blocked'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const CARD_WAIT = { selector: '[data-testid="recovery-card"]' }

const PREFIX = '[Tool blocked — reason sent to the agent]'
const CAUSE = 'invalid_name'
const NOTICE = [
  '[Kiro Crew host notice] The tool call you just made was blocked before it ran.',
  '',
  'Blocked: fs_read_file — the tool name is not one this host exposes.',
  '',
  'Use one of the tools listed for this session, or say why you cannot proceed.',
].join('\n')

const t0 = Date.now() / 1000 - 600
const slots = [
  {
    key: SLOT,
    title: 'Summarise the release notes',
    running: false,
    last_message: 'Re-reading the file with the tool this host exposes.',
    messages: 4,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

const detail = {
  running: false,
  has_more: false,
  total: 4,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Summarise the release notes in docs/releases/latest.md.' },
    { role: 'assistant', ts: t0 + 8, content: 'Reading the release notes now.' },
    // VERBATIM gateway format: prefix, space, cause token, newline, notice.
    { role: 'inject', ts: t0 + 12, content: `${PREFIX} ${CAUSE}\n${NOTICE}`, meta: {} },
    {
      role: 'assistant',
      ts: t0 + 20,
      content: 'That tool name is not available here — re-reading the file with the one this session exposes.',
    },
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
   * `inject` rows are not in TurnBlock's always-visible set, so with
   * collapse-reasoning on they sit inside the "Worked through N steps" pane.
   * Open it so the card is framed where a user actually reads it.
   */
  async function expandTurn() {
    const toggle = page.getByRole('button', { name: /Worked through \d+ steps/ })
    if (await toggle.count()) {
      await toggle.first().evaluate(el => el.click())
      await page.waitForTimeout(500)
    }
  }

  /**
   * The transcript is virtualized: rows are absolutely positioned and a
   * neighbouring row's box can sit over the card, so Playwright's hit-testing
   * click times out. Dispatching on the node still runs the real React
   * onClick — which is the surface under test — without depending on the
   * virtualizer's stacking.
   */
  async function expandCard() {
    await page
      .locator('[data-testid="recovery-card"][data-kind="tool_blocked"] [data-testid="recovery-card-toggle"]')
      .first()
      .evaluate(el => {
        el.scrollIntoView({ block: 'center' })
        el.click()
      })
    await page.waitForTimeout(500)
  }

  /**
   * Pin the locale to English.
   *
   * The harness's own init script CLEARS localStorage on every navigation,
   * so writing the key on the page and reloading loses it again. Init
   * scripts run in registration order, so this one is registered after the
   * harness's first navigation and therefore runs after its clear on the
   * reload. Without the pin the SPA negotiates a language from the
   * environment and the shot comes out in whatever the runner speaks.
   */
  let localePinned = false
  async function loadInEnglish(theme) {
    await load(theme, CARD_WAIT)
    if (!localePinned) {
      await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
      localePinned = true
    }
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector(CARD_WAIT.selector, { timeout: 20000 })
    await page.waitForTimeout(800)
  }

  for (const theme of ['light', 'dark']) {
    await loadInEnglish(theme)
    await expandTurn()
    await expandCard()
    const card = page.locator('[data-testid="recovery-card"][data-kind="tool_blocked"]').first()
    await card.screenshot({ path: `${OUT}/expanded-${theme}.png` })
    console.log('wrote', `${OUT}/expanded-${theme}.png`)
  }

  await close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
