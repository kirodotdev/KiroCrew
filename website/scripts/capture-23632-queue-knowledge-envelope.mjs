/**
 * Screenshot evidence for #23632: a message sent with knowledge context while
 * the agent is busy is queued with its wire text, which opens with the
 * `[KNOWLEDGE CONTEXT ...]` envelope. The queue card used to show that
 * envelope instead of what the user typed.
 *
 * Runs the REAL SPA (fixture shape of capture-queue-edit-multiline.mjs): a
 * persisted 2-pane split whose background pane carries ONE queued entry whose
 * content is the exact wire text `buildOutgoingTurn` produces for a typed line
 * plus a picked knowledge block. The frame ASSERTS what the card shows:
 *   MODE=after  (default) the card text is the typed line only
 *   MODE=before run against a checkout of the base commit: the card text
 *               starts with the envelope -- the defect
 *
 * Usage (from website/):
 *   node scripts/capture-23632-queue-knowledge-envelope.mjs http://127.0.0.1:6841 ../temp-screenshots/23632-queue-knowledge
 *   MODE=before node scripts/capture-23632-queue-knowledge-envelope.mjs http://127.0.0.1:6842 ../temp-screenshots/23632-queue-knowledge
 */
import { chromium } from 'playwright'
import {
  TWO_PANE_SPLIT_LAYOUTS,
  jsonResponder as json,
  makeChecker,
  prepareSplitChatPage,
  queuedPaneFixtures,
  splitPaneFixtures,
} from './lib/prepare-split-chat-page.mjs'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/23632-queue-knowledge'
const MODE = process.env.MODE === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const TYPED = 'Summarize the rollout runbook for me.'
const QUEUED = '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n\n## Rollout runbook\nStep 1: freeze deploys. Step 2: drain the canary.\n\n[END KNOWLEDGE CONTEXT]\n\n' + TYPED

const { slots, detailA, detailB } = queuedPaneFixtures(now, QUEUED)
const splitLayouts = TWO_PANE_SPLIT_LAYOUTS
const FIXTURES = splitPaneFixtures(slots)
const { check, failed } = makeChecker()

async function capture(theme) {
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await prepareSplitChatPage(ctx, { base: BASE, fixtures: FIXTURES, detailA, detailB, splitLayouts, json, theme })
  const card = page.getByTestId('queue-card')
  await card.first().waitFor({ state: 'visible', timeout: 15000 })
  const text = (await card.first().textContent()) || ''
  const applied = await page.evaluate(() => document.documentElement.dataset.mode)
  check(`${theme} theme applied`, applied === theme, `data-mode=${applied}`)
  if (MODE === 'after') {
    check(`${theme} card shows the typed line`, text.includes(TYPED), JSON.stringify(text))
    check(`${theme} card hides the envelope`, !text.includes('KNOWLEDGE CONTEXT') && !text.includes('Rollout runbook\nStep'), JSON.stringify(text))
  } else {
    check(`${theme} card shows the envelope (defect)`, text.includes('KNOWLEDGE CONTEXT'), JSON.stringify(text))
  }
  // Frame the background pane (the one holding the editor) so the composite
  // shows the editor and the card around it, not the whole split.
  const pane = page.locator('[data-chat-pane]').nth(1)
  await pane.screenshot({ path: `${OUT}/${theme}-${MODE}.png` })
  await ctx.close()
  await browser.close()
}

await capture('light')
await capture('dark')
if (failed()) { console.error('frame assertions failed'); process.exit(1) }
console.log(`all frames verified (${MODE}) →`, OUT)
