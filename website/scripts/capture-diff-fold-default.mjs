/**
 * Screenshot harness for FOLD-BY-DEFAULT across every diff surface in the chat
 * transcript.
 *
 * The point of the shots is a first paint: nothing here clicks anything before
 * `01-all-folded.png`, so that frame is what a reader sees on arrival. It has to
 * show all three surfaces folded at once, because each has its own independent
 * control and #8004 only closed one of them:
 *
 *   1. a tool-call diff card      → `tool-diff-chip`         (ToolCallLine)
 *   2. an over-cap tool diff      → `tool-diff-summary-chip` (presentToolDiff)
 *   3. a prose ```diff fence      → `prose-diff-chip`        (FoldableDiffBlock)
 *
 * The later frames prove the fold is a fold and not a drop: one click on a chip
 * opens the patch it stands for, and opening one leaves the others alone.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness
 * with every /api/** call answered from fixtures — no gateway, no token, no
 * agent — so the virtualizer, Pierre's diff renderer and both fold registries
 * behave exactly as in production.
 *
 * Usage: node scripts/capture-diff-fold-default.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/diff-fold-default'
const SLOT = 'chat-diff-fold-default'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const CHIP_WAIT = { selector: '[data-testid="tool-diff-chip"]', settle: 900 }

/** A small unified diff — under presentToolDiff's 400-line cap, so a card. */
const CARD_DIFF = [
  '--- a/website/src/pages/chat/ToolCallLine.tsx',
  '+++ b/website/src/pages/chat/ToolCallLine.tsx',
  '@@ -318,9 +318,11 @@',
  '   const toolCallId = message.meta?.tool_call_id as string | undefined',
  '-  const [cardFolded, setCardFolded] = useState(false)',
  '+  const [cardFolded, setCardFolded] = useState(',
  '+    () => !(toolCallId && openedDiffCards.has(toolCallId)),',
  '+  )',
  '   const toggleCardFolded = useCallback(() => {',
  '     setCardFolded(prev => {',
  '       const next = !prev',
  '@@ -331,8 +333,8 @@',
  '       if (toolCallId) {',
  '-        if (next) foldedDiffCards.add(toolCallId)',
  '-        else foldedDiffCards.delete(toolCallId)',
  '+        if (next) openedDiffCards.delete(toolCallId)',
  '+        else openedDiffCards.add(toolCallId)',
  '       }',
  '       return next',
].join('\n')

/**
 * A whole-file create: one giant all-additions diff, over the card cap. It
 * degrades to the summary chip — a surface that was ALREADY chip-only, included
 * so the folded frame shows the two tool-row chips side by side and a reviewer
 * can see the new fold matches the shape the cap already produced.
 */
const OVER_CAP_DIFF = [
  '--- /dev/null',
  '+++ b/website/src/generated/themeTokens.ts',
  '@@ -0,0 +1,612 @@',
  ...Array.from({ length: 612 }, (_, i) => `+  token${i}: 'var(--mc-token-${i})',`),
].join('\n')

const fence = (path, body) => ['```diff', `--- a/${path}`, `+++ b/${path}`, ...body, '```'].join('\n')

/** The assistant's own retelling of the change, as ```diff fences in prose. */
const PROSE = [
  'Both halves now fold. The card default is inverted, and the registry it',
  'consults is inverted with it — a registry of *folds* would have re-opened',
  'every card the virtualizer re-mounted.',
  '',
  fence('website/src/components/FoldableDiffBlock.tsx', [
    '@@ -18,6 +18,6 @@',
    "-  * Deliberately NOT the same control as `ToolCallLine`'s card fold: that",
    '-  * one defaults OPEN, because it is the only record of that edit.',
    "+  * A separate control from `ToolCallLine`'s card fold, which governs a diff",
    '+  * the dashboard itself rendered from a tool call. Both default CLOSED.',
  ]),
  '',
  fence('website/src/pages/chat/toolDiff.ts', [
    '@@ -19,4 +19,4 @@',
    '-  * EVERY edit diff gets a visible trace. Small diffs render the full card;',
    '+  * EVERY edit diff gets a visible trace. Small diffs render a card, folded',
    '+  * to its chip until the reader opens it;',
  ]),
  '',
  'All 174 frontend tests pass.',
].join('\n')

const t0 = Date.now() / 1000 - 900

const slots = [
  {
    key: SLOT,
    title: 'Fold the tool-call diff card by default',
    running: false,
    last_message: 'Both halves now fold.',
    messages: 5,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

/**
 * `meta.kind` / `meta.input` are what a PERSISTED edit row carries (see
 * `_tool_meta` in chat_runner.py) and are exactly what `presentToolDiff` reads,
 * so these rows exercise the historical-row path rather than the live toolLog.
 */
const detail = {
  running: false,
  has_more: false,
  total: 5,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Make the folded diff the default, and regenerate the theme tokens.' },
    { role: 'assistant', ts: t0 + 6, content: 'Inverting the card default and its registry.' },
    {
      role: 'tool',
      ts: t0 + 14,
      content: '🔧 fs_write',
      cls: '',
      meta: { tool_call_id: 'tc_card', kind: 'edit', input: CARD_DIFF, purpose: 'Fold the card by default' },
    },
    {
      role: 'tool',
      ts: t0 + 22,
      content: '🔧 fs_write',
      cls: '',
      meta: { tool_call_id: 'tc_overcap', kind: 'edit', input: OVER_CAP_DIFF, purpose: 'Regenerate the theme tokens' },
    },
    { role: 'assistant', ts: t0 + 40, content: PROSE },
  ],
}

async function main() {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
    viewport: { width: 1280, height: 1100 },
  })

  /**
   * The transcript is virtualized: rows are absolutely positioned and a
   * neighbouring row's box can cover a chip, so Playwright's hit-testing click
   * times out on a target that is plainly visible. Dispatching on the node runs
   * the same React onClick — the surface under test — without depending on the
   * virtualizer's stacking.
   */
  async function clickChip(testid) {
    await page.locator(`[data-testid="${testid}"]`).first().evaluate(el => {
      el.scrollIntoView({ block: 'center' })
      el.click()
    })
    await page.waitForTimeout(700)
  }

  async function shot(name) {
    // Park the cursor off-canvas: a chip left under the pointer photographs in
    // its hover colour, which reads as a selected state it does not have.
    await page.mouse.move(0, 0)
    await page.waitForTimeout(200)
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  await load('dark', CHIP_WAIT)
  /**
   * Pin the locale to English. The harness's init script CLEARS localStorage on
   * every navigation, so a key written on the page is lost on reload. Init
   * scripts run in registration order, so registering this one AFTER the first
   * navigation puts it after that clear on the reload.
   */
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector(CHIP_WAIT.selector, { timeout: 20000 })
  await page.waitForTimeout(900)

  // Counted, not just photographed: a chip missing from the first paint would
  // otherwise read as "folded" in the frame when it is really "not rendered".
  for (const testid of ['tool-diff-chip', 'tool-diff-summary-chip', 'prose-diff-chip']) {
    console.log(testid, await page.locator(`[data-testid="${testid}"]`).count())
  }
  await shot('01-all-folded')

  await clickChip('tool-diff-chip')
  await shot('02-tool-card-open')

  await clickChip('prose-diff-chip')
  await shot('03-tool-card-and-prose-open')

  await close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
