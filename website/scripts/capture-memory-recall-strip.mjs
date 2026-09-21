/**
 * Screenshot harness for the transcript's recalled-memory strip, in its three shapes.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness, with
 * every /api/** call answered from fixtures. No gateway, no agent, no Jev: only the
 * network is stubbed, so `readMemoryRecallRecord`, the transcript virtualizer and the
 * strip itself render exactly as in production.
 *
 * Four fixtures, because the strip has four states a reader acts on differently:
 * a turn where Jev DROPPED some of the shortlist (the common case, and the one the
 * counts exist for), one where it kept everything (so "similarity 6 · Jev kept 6"
 * can be compared against the narrowed line rather than described), one where it kept
 * NOTHING (the floor of the range: an empty kept list, which the expanded panel names
 * rather than leaving blank), and one where the decision FAILED (the error surface and
 * its agent hand-off). The record rides `meta.decisions_strip`, which is where a
 * transcript reloaded from history carries it.
 *
 * `/api/decisions/consent` is answered ON even though the strip does not read it, for
 * the reason the sibling `capture-decision-strip.mjs` gives: the Settings card shares
 * that query key, and answering it keeps the boot fixture honest about the state being
 * photographed. The strip draws from the record alone.
 *
 * Usage: node scripts/capture-memory-recall-strip.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/memory-recall-strip'
const SLOT = 'chat-memory-recall'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const STRIP_WAIT = { selector: '[data-testid="memory-recall-strip"]' }

/** Consent is on and pointed at the address it was given for. */
const CONSENT = {
  enabled: true,
  endpoint: 'https://api.typesafe.ai/v1/systemone',
  configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
  permits: true,
  tool_args: false,
  memory_text: true,
}

/** Six memories were recalled by similarity; Jev kept three. */
const NARROWED = {
  turn_id: 'turn-7d1e04',
  ts: '2026-09-21T07:04:11Z',
  point: 'memory.recall',
  latency_ms: 210,
  baseline_keys: ['mem-4f2a9c', 'mem-8b71d0', 'mem-1c34ee', 'mem-90ab52', 'mem-2d6f18', 'mem-55e7c1'],
  jev_keys: ['mem-4f2a9c', 'mem-1c34ee', 'mem-55e7c1'],
  p: 0.81,
  chars_saved: 2100,
  candidates: 6,
  message_chars: 96,
  error: null,
}

/** Jev kept every memory similarity recalled. */
const KEPT_ALL = {
  ...NARROWED,
  turn_id: 'turn-91c3ab',
  jev_keys: [...NARROWED.baseline_keys],
  p: 0.93,
  chars_saved: 0,
  latency_ms: 164,
}

/**
 * Jev kept NOTHING: every recalled memory was dropped, so the prompt carried none.
 * The extreme of the same shape rather than a separate state -- worth photographing
 * because the expanded panel has to NAME an empty kept list rather than draw a blank
 * where the ids go, and because the counts read "Jev kept 0" with the largest saving.
 */
const KEPT_NONE = {
  ...NARROWED,
  turn_id: 'turn-3ea8f7',
  jev_keys: [],
  p: 0.12,
  chars_saved: 4300,
  latency_ms: 188,
}

/** The decision failed, so the shortlist went in unchanged and the strip says why. */
const FAILED = {
  ...NARROWED,
  turn_id: 'turn-c05e2f',
  jev_keys: [...NARROWED.baseline_keys],
  p: null,
  chars_saved: 0,
  latency_ms: 1002,
  error: 'timeout',
}

const t0 = Date.now() / 1000 - 900

const slots = [
  {
    key: SLOT,
    title: 'Where do we deploy the signer',
    running: false,
    last_message: 'us-west-2, and the rollback runbook is in the ops repo.',
    messages: 6,
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
  total: 6,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Where do we deploy the signer, and who owns the rollback?' },
    {
      role: 'assistant',
      ts: t0 + 22,
      content: 'us-west-2, and the rollback runbook is in the ops repo under `runbooks/signer.md`.',
      meta: { decisions_strip: NARROWED },
    },
    { role: 'user', ts: t0 + 120, content: 'Remind me what we decided about the signing key rotation.' },
    {
      role: 'assistant',
      ts: t0 + 149,
      content: 'Ninety days, rotated by the pipeline rather than by hand, with the old key kept readable for one cycle.',
      meta: { decisions_strip: KEPT_ALL },
    },
    { role: 'user', ts: t0 + 300, content: 'And the distribution bucket — is it the same account?' },
    {
      role: 'assistant',
      ts: t0 + 331,
      content: 'Same account, separate stack. The CDN reads it through an origin-access identity rather than a public policy.',
      meta: { decisions_strip: FAILED },
    },
    // LAST, and the only other `data-agree=false` row, so the two selectors below stay
    // unambiguous: `.first()` is the narrowed row and `.last()` is this one.
    { role: 'user', ts: t0 + 420, content: 'What did we say about the staging alarm thresholds?' },
    {
      role: 'assistant',
      ts: t0 + 447,
      content: 'Nothing on record for staging — the thresholds you are thinking of are the production ones.',
      meta: { decisions_strip: KEPT_NONE },
    },
  ],
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({ slot: SLOT, project: PROJECT, slots, detail })

  // Registered AFTER the harness's own catch-all, so it wins: Playwright matches
  // route handlers in reverse registration order.
  await page.route('**/api/decisions/consent', route => json(route, CONSENT))

  /**
   * Pin the locale to English. The harness's init script CLEARS localStorage on every
   * navigation, so writing the key and reloading loses it again; init scripts run in
   * registration order, so this one is registered after the harness's first navigation
   * and therefore runs after its clear on the reload.
   */
  let localePinned = false
  async function loadInEnglish(theme) {
    await load(theme, STRIP_WAIT)
    if (!localePinned) {
      await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
      localePinned = true
    }
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForSelector(STRIP_WAIT.selector, { timeout: 20000 })
    await page.waitForTimeout(800)
  }

  /**
   * The transcript is virtualized: rows are absolutely positioned and a neighbouring
   * row's box can sit over the strip, so Playwright's hit-testing click times out.
   * Dispatching on the node still runs the real React onClick — which is the surface
   * under test — without depending on the stacking.
   */
  async function open(selector) {
    await page.locator(`${selector} [data-testid="memory-recall-strip-toggle"]`).first().evaluate(el => {
      el.scrollIntoView({ block: 'center' })
      el.click()
    })
    await page.waitForTimeout(500)
  }

  async function shoot(selector, name) {
    const row = page.locator(selector).first()
    await row.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await row.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // The three rows are told apart by `data-agree`, which is set equality of the two
  // lists, plus the error one's own turn. `data-agree=false` is the narrowed row;
  // the two `true` rows are distinguished by DOM order, which is transcript order.
  // Two rows carry `data-agree=false`: the narrowed one is FIRST in transcript order
  // and the kept-none one is LAST, which is what the fixture order above is for.
  const NARROWED_SEL = '[data-testid="memory-recall-strip"][data-agree="false"]'
  const AGREED_SEL = '[data-testid="memory-recall-strip"][data-agree="true"]'

  for (const theme of ['light', 'dark']) {
    await loadInEnglish(theme)

    await shoot(NARROWED_SEL, `collapsed-narrowed-${theme}`)
    await shoot(AGREED_SEL, `collapsed-kept-all-${theme}`)

    // Open: both id lists, the candidate count, the egress and the latency, plus the
    // similarity ranker's own thumbs pair.
    await open(NARROWED_SEL)
    await shoot(NARROWED_SEL, `expanded-${theme}`)

    // Kept none, expanded: the panel has to NAME the empty kept list. Driven through
    // the locator rather than `open`/`shoot`, which both take `.first()`.
    const keptNoneRow = page.locator(NARROWED_SEL).last()
    await keptNoneRow.locator('[data-testid="memory-recall-strip-toggle"]').evaluate(el => {
      el.scrollIntoView({ block: 'center' })
      el.click()
    })
    await page.waitForTimeout(500)
    await keptNoneRow.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await keptNoneRow.screenshot({ path: `${OUT}/expanded-kept-none-${theme}.png` })
    console.log('wrote', `${OUT}/expanded-kept-none-${theme}.png`)

    // The failed decision, expanded: the error surface and its agent hand-off. It is
    // the LAST strip in the transcript, so `.last()` selects it without depending on
    // the two agreed rows' relative order.
    const failedRow = page.locator(AGREED_SEL).last()
    await failedRow.locator('[data-testid="memory-recall-strip-toggle"]').evaluate(el => {
      el.scrollIntoView({ block: 'center' })
      el.click()
    })
    await page.waitForTimeout(500)
    await failedRow.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await failedRow.screenshot({ path: `${OUT}/expanded-error-${theme}.png` })
    console.log('wrote', `${OUT}/expanded-error-${theme}.png`)
  }

  await close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
