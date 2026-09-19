/**
 * Screenshot harness for the transcript's decision strip, collapsed and open.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness,
 * with every /api/** call answered from fixtures. No gateway, no agent, no Jev:
 * only the network is stubbed, so `readDecisionStrip`, the consent gate, the
 * transcript virtualizer and the strip itself render exactly as in production.
 *
 * Two fixtures, because the strip has two shapes and they say different things:
 * a turn where the two sides picked DIFFERENT skills (both named, the divergence
 * being the only thing on the line a reader can act on) and one where they
 * agreed (one list, one muted check). The record rides `meta.decisions_strip`,
 * which is where a transcript reloaded from history carries it.
 *
 * `/api/decisions/consent` is answered ON even though the strip does not read it:
 * the Settings card shares that query key, and answering it keeps the boot fixture
 * honest about the state being photographed — a receipt for a turn the seam really
 * decided. The strip draws from the record alone.
 *
 * Usage: node scripts/capture-decision-strip.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decision-strip'
const SLOT = 'chat-decision-strip'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const STRIP_WAIT = { selector: '[data-testid="decision-strip"]' }

/** Consent is on and pointed at the address it was given for. */
const CONSENT = {
  enabled: true,
  endpoint: 'https://api.typesafe.ai/v1/systemone',
  configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
  permits: true,
}

/** The two sides picked different skills, and one answer was refused. */
const DISAGREED = {
  turn_id: 'turn-4f2a9c',
  ts: '2026-09-19T07:04:11Z',
  point: 'skills.select',
  baseline: ['brazil', 'crux-code-reviews'],
  jev: ['brazil'],
  agree: false,
  p: 0.81,
  tokens_saved: 3240,
  candidates: 42,
  batches: 3,
  history_chars: 1840,
  truncated: 2,
  dropped: [{ key: 'tst', p: 0.12 }],
  error: null,
}

/** The two sides picked the same skill. */
const AGREED = {
  ...DISAGREED,
  turn_id: 'turn-7b1d03',
  baseline: ['brazil'],
  jev: ['brazil'],
  agree: true,
  p: 0.94,
  tokens_saved: 0,
  truncated: 0,
  dropped: [],
}

const t0 = Date.now() / 1000 - 600

const slots = [
  {
    key: SLOT,
    title: 'Build the package and read the failures',
    running: false,
    last_message: 'Built the package. Two tests fail, both in the same file.',
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
    { role: 'user', ts: t0, content: 'Build the package and tell me what fails.' },
    {
      role: 'assistant',
      ts: t0 + 24,
      content: 'Built the package. Two tests fail, both in `test_loader.py`, and both on the same missing fixture.',
      meta: { decisions_strip: DISAGREED },
    },
    { role: 'user', ts: t0 + 60, content: 'Open a review for the fix.' },
    {
      role: 'assistant',
      ts: t0 + 88,
      content: 'Opened the review. One commit, sitting on a fresh base.',
      meta: { decisions_strip: AGREED },
    },
  ],
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({ slot: SLOT, project: PROJECT, slots, detail })

  // Registered AFTER the harness's own catch-all, so it wins: Playwright matches
  // route handlers in reverse registration order.
  await page.route('**/api/decisions/consent', route => json(route, CONSENT))

  /**
   * Pin the locale to English.
   *
   * The harness's init script CLEARS localStorage on every navigation, so
   * writing the key and reloading loses it again. Init scripts run in
   * registration order, so this one is registered after the harness's first
   * navigation and therefore runs after its clear on the reload.
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
   * The transcript is virtualized: rows are absolutely positioned and a
   * neighbouring row's box can sit over the strip, so Playwright's hit-testing
   * click times out. Dispatching on the node still runs the real React onClick —
   * which is the surface under test — without depending on the stacking.
   */
  async function open(selector) {
    await page.locator(`${selector} [data-testid="decision-strip-toggle"]`).first().evaluate(el => {
      el.scrollIntoView({ block: 'center' })
      el.click()
    })
    await page.waitForTimeout(500)
  }

  const DISAGREE_SEL = '[data-testid="decision-strip"][data-agree="false"]'
  const AGREE_SEL = '[data-testid="decision-strip"][data-agree="true"]'

  for (const theme of ['light', 'dark']) {
    await loadInEnglish(theme)

    // Collapsed: both strips in one shot, so the agreed and diverged lines are
    // comparable rather than described.
    const transcript = page.locator(DISAGREE_SEL).first()
    await transcript.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await transcript.screenshot({ path: `${OUT}/collapsed-diverged-${theme}.png` })
    console.log('wrote', `${OUT}/collapsed-diverged-${theme}.png`)

    const agreed = page.locator(AGREE_SEL).first()
    await agreed.evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.waitForTimeout(300)
    await agreed.screenshot({ path: `${OUT}/collapsed-agreed-${theme}.png` })
    console.log('wrote', `${OUT}/collapsed-agreed-${theme}.png`)

    // Open: the question's own shape, the refusal, and the baseline's thumbs.
    await open(DISAGREE_SEL)
    await page.locator(DISAGREE_SEL).first().screenshot({ path: `${OUT}/expanded-${theme}.png` })
    console.log('wrote', `${OUT}/expanded-${theme}.png`)
  }

  await close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
