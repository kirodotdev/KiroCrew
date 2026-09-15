/**
 * Screenshot harness for the injected-notice rows this change fixes.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call and the /api/ws websocket answered from fixtures. No gateway, no
 * token, no agent. The client code is unmodified — only the network is stubbed —
 * so the transcript, its virtualizer and the cards render exactly as in
 * production.
 *
 * Three rows are under test, and the fixture carries one of each:
 *
 *   1. `synthesis`  — the post-fan-out consolidation prompt, using the verbatim
 *      SUBAGENT_SYNTHESIS_PREFIX the gateway prepends. Before this change the
 *      gateway appended NO row for it at all, so it reached the conversation log
 *      unattributed and replayed as though the user had typed it.
 *   2. `generic`    — an inject shape this build has no prefix for. Before this
 *      change the `inject` branch was non-terminal, so anything unrecognised fell
 *      through to the full-width bubble renderer.
 *   3. a CRON row   — role `inject` as well, but carrying meta.cronLabel. It must
 *      still paint as a labelled bubble, NOT be swallowed by the new fallback.
 *      This is the regression the full suite caught, so it is shot deliberately.
 *
 * Usage: node scripts/capture-inject-notice.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/inject-notice'
const SLOT = 'chat-inject-notice'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** The cards are what the shots are of, so every load waits for one to mount. */
const CARD_WAIT = { selector: '[data-testid="recovery-card"]' }

// Verbatim wire values from src/kiro_crew/dashboard/state.py. Matched with
// startsWith and then sliced off, so a mismatch here is real drift.
const SYNTHESIS = '[SYSTEM] Sub-agent synthesis:'

const synthesisBody =
  `${SYNTHESIS} all sub-agents you spawned have completed and each result was processed above. ` +
  'Produce a single consolidated synthesis as your reply for the user: (1) restate the original ' +
  'goal you spawned the sub-agents for, (2) synthesize the combined findings across all of them ' +
  '(do not just repeat each result in turn), and (3) give concrete recommended next actions or ' +
  'decisions. This is the user-facing deliverable — keep it clear and actionable.'

// A marker this build deliberately does not know, standing in for a gateway
// newer than the frontend. It must render as a folded note, never as speech.
const unknownBody =
  '[Context budget — automatic notice]\n' +
  'This session crossed its context threshold and older turns were compacted. Nothing you ' +
  'already committed was lost; re-read the summary above before continuing.'

const t0 = Date.now() / 1000 - 900
const slots = [{
  key: SLOT,
  title: 'Audit the resolver across all three adapters',
  running: false,
  last_message: 'All three adapters agree on the ranking rule.',
  messages: 8,
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
  total: 8,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Audit the adapter resolver in parallel and tell me whether the three agree.' },
    { role: 'assistant', ts: t0 + 10, content: 'Spawning three agents, one per adapter. Waiting for their completion events.' },
    {
      role: 'subagent',
      ts: t0 + 200,
      content: '[Subagent completion event] Agent a41c9f completed ✅\nTask: audit the codex adapter resolution ladder',
      meta: {},
    },
    // The row this change adds. Renders as a collapsed one-liner via RecoveryCard.
    { role: 'inject', ts: t0 + 205, content: synthesisBody, meta: { injectKind: 'synthesis' } },
    { role: 'assistant', ts: t0 + 240, content: 'All three adapters resolve through the same ranking rule: a concrete supported install outranks a version-agnostic shim.' },
    // An inject shape with no dedicated card, but positively stamped as
    // gateway-authored — must fold, not become a bubble.
    { role: 'inject', ts: t0 + 500, content: unknownBody, meta: { injectKind: 'recovery' } },
    // Role `inject` too, but a cron row: must stay a labelled bubble. Stamped in
    // `meta` (durable) as well as the legacy `cls`-derived cronLabel.
    {
      role: 'inject',
      ts: t0 + 700,
      content: 'Nightly adapter audit: re-run the resolver probe and report any adapter whose Node floor regressed.',
      meta: { injectKind: 'cron', cronLabel: 'nightly-adapter-audit' },
    },
    // A replay of the USER'S OWN words on the recovery path. Speech, not a note —
    // the case a content-sniffing fallback could not distinguish.
    {
      role: 'inject',
      ts: t0 + 710,
      content: 'Also confirm the goose adapter delegates terminal calls.',
      meta: { injectKind: 'user_replay' },
    },
    { role: 'assistant', ts: t0 + 720, content: 'Probe re-run: no adapter regressed its Node floor.' },
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
   * exactly where they sit today. Open it so the shots frame the cards in the
   * position a user reads them.
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
   * Element-scoped shots of one card kind, collapsed then expanded.
   *
   * The transcript is virtualized: rows are absolutely positioned and a
   * neighbouring row's box can sit over the card, so Playwright's hit-testing
   * click times out. Dispatching the click on the node itself still runs the
   * real React onClick — which is what is under test — without depending on the
   * virtualizer's stacking. Toggled back afterwards so a following viewport shot
   * still captures the state it expects.
   */
  async function kindShots(kind, theme) {
    const card = page.locator(`[data-testid="recovery-card"][data-kind="${kind}"]`).first()
    if (!(await card.count())) {
      throw new Error(`no ${kind} card rendered — fixture or PREFIXES drift`)
    }
    const toggle = card.getByTestId('recovery-card-toggle').first()
    await card.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await card.screenshot({ path: `${OUT}/${kind}-collapsed-${theme}.png` })
    console.log('wrote', `${OUT}/${kind}-collapsed-${theme}.png`)

    await toggle.evaluate(el => el.click())
    await page.waitForTimeout(500)
    await card.screenshot({ path: `${OUT}/${kind}-expanded-${theme}.png` })
    console.log('wrote', `${OUT}/${kind}-expanded-${theme}.png`)

    await toggle.evaluate(el => el.click())
    await page.waitForTimeout(300)
  }

  /**
   * Non-vacuous assertions. A screenshot proves nothing on its own — if the cards
   * silently stopped rendering the shots would still be written, just empty. So
   * assert the exact set of kinds, and assert the cron row did NOT become a card.
   */
  async function assertShape() {
    const kinds = await page.getByTestId('recovery-card').evaluateAll(els =>
      els.map(e => e.dataset.kind).sort(),
    )
    const expected = ['generic', 'synthesis']
    if (JSON.stringify(kinds) !== JSON.stringify(expected)) {
      throw new Error(`expected cards ${expected.join(',')} but rendered ${kinds.join(',') || '(none)'}`)
    }
    // The cron carve-out: its label renders, and it is not one of the cards above.
    const cronVisible = await page.getByText('nightly-adapter-audit').count()
    if (!cronVisible) {
      throw new Error('cron row lost its label — the fallback swallowed it')
    }
    // The user's own replayed words must still be on screen as speech, not folded
    // behind a disclosure. This is the case only a provenance stamp can decide.
    const replayVisible = await page.getByText('Also confirm the goose adapter delegates').count()
    if (!replayVisible) {
      throw new Error('user_replay row was folded away — it is the user speech')
    }
    // Neither notice may leak its machine prose as a full-width bubble.
    const leaked = await page.evaluate(() =>
      Array.from(document.querySelectorAll('.msg-inject, [data-role="inject"]')).filter(el =>
        el.textContent?.includes('[SYSTEM] Sub-agent synthesis:'),
      ).length,
    )
    if (leaked) throw new Error('synthesis prompt leaked into a bubble')
    console.log('assertions passed — kinds:', kinds.join(','), '| cron label present')
  }

  await load('dark', CARD_WAIT)
  await expandTurn()
  await assertShape()
  await shot('transcript-dark')
  await kindShots('synthesis', 'dark')
  await kindShots('generic', 'dark')

  await load('light', CARD_WAIT)
  await expandTurn()
  await assertShape()
  await shot('transcript-light')
  await kindShots('synthesis', 'light')
  await kindShots('generic', 'light')

  await close()
}

main().catch(err => { console.error(err); process.exit(1) })
