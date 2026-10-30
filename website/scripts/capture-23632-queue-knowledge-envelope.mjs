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
 *   MODE=after  (default) the card text is the typed line only, the card
 *               wears the "knowledge" chip, and the pencil editor
 *               (`<theme>-editor-after.png`) opens on the typed line alone.
 *               Then a second entry whose knowledge item holds the close
 *               marker on its own line (`<theme>-fallback-after.png`): two
 *               close-marker lines, so the split refuses and the card keeps
 *               the whole text, with no chip -- the same as main
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
const OPEN = '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n\n'
const CLOSE = '[END KNOWLEDGE CONTEXT]\n\n'
const QUEUED = OPEN + '## Rollout runbook\nStep 1: freeze deploys. Step 2: drain the canary.\n\n' + CLOSE + TYPED
// A knowledge item that quotes the close marker on a line of its own: the wire
// then holds two close-marker lines and the real end cannot be told.
const FALLBACK = OPEN + '## Marker reference\nExample:\n[END KNOWLEDGE CONTEXT]\nKeep this paragraph.\n\n' + CLOSE + 'Summarize the reference.'

const splitLayouts = TWO_PANE_SPLIT_LAYOUTS
const { check, failed } = makeChecker()

async function openPage(theme, queued, width = 1440) {
  const { slots, detailA, detailB } = queuedPaneFixtures(now, queued)
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await prepareSplitChatPage(ctx, { base: BASE, fixtures: splitPaneFixtures(slots), detailA, detailB, splitLayouts, json, theme })
  const card = page.getByTestId('queue-card')
  await card.first().waitFor({ state: 'visible', timeout: 15000 })
  const applied = await page.evaluate(() => document.documentElement.dataset.mode)
  check(`${theme} theme applied`, applied === theme, `data-mode=${applied}`)
  const close = async () => { await ctx.close(); await browser.close() }
  return { page, card, close }
}

async function captureFallback(theme) {
  const { page, card, close } = await openPage(theme, FALLBACK)
  const text = (await card.first().textContent()) || ''
  check(`${theme} fallback card keeps the whole text`, text.includes('KNOWLEDGE CONTEXT'), JSON.stringify(text))
  check(`${theme} fallback card wears no knowledge chip`, (await page.getByTestId('queue-card-knowledge').count()) === 0, 'chip present')
  await page.locator('[data-chat-pane]').nth(1).screenshot({ path: `${OUT}/${theme}-fallback-after.png` })
  await close()
}

// A narrow split pane: the card sits beside its action buttons, so the chip
// must drop to its icon and leave the preview room for the typed words.
async function captureNarrow(theme) {
  const { page, close } = await openPage(theme, QUEUED, 760)
  const pane = page.locator('[data-chat-pane]').nth(1)
  const cardBox = await page.getByTestId('queue-card').first().boundingBox()
  const label = await page.getByTestId('queue-card-knowledge-label').first().boundingBox()
  const preview = await page.getByTestId('queue-card').first().locator('span.truncate.flex-1').boundingBox()
  check(`${theme} narrow card is under the label breakpoint`, !!cardBox && cardBox.width < 360, JSON.stringify(cardBox))
  check(`${theme} narrow chip drops its label`, !label || label.width <= 1, JSON.stringify(label))
  check(`${theme} narrow preview keeps room for the words`, !!preview && preview.width >= 80, JSON.stringify(preview))
  await pane.screenshot({ path: `${OUT}/${theme}-narrow-after.png` })
  await close()
}

async function capture(theme) {
  const { page, card, close } = await openPage(theme, QUEUED)
  const text = (await card.first().textContent()) || ''
  if (MODE === 'after') {
    check(`${theme} card shows the typed line`, text.includes(TYPED), JSON.stringify(text))
    check(`${theme} card hides the envelope`, !text.includes('KNOWLEDGE CONTEXT') && !text.includes('Rollout runbook\nStep'), JSON.stringify(text))
    check(`${theme} card wears the knowledge chip`, await page.getByTestId('queue-card-knowledge').first().isVisible(), 'chip missing')
    // The usual pane width shows the chip WITH its label: a bare book icon
    // beside the action buttons does not say what it means.
    const wideLabel = await page.getByTestId('queue-card-knowledge-label').first().boundingBox()
    check(`${theme} chip label visible at the usual pane width`, !!wideLabel && wideLabel.width > 40, JSON.stringify(wideLabel))
  } else {
    check(`${theme} card shows the envelope (defect)`, text.includes('KNOWLEDGE CONTEXT'), JSON.stringify(text))
  }
  // Frame the background pane (the one holding the editor) so the composite
  // shows the editor and the card around it, not the whole split.
  const pane = page.locator('[data-chat-pane]').nth(1)
  await pane.screenshot({ path: `${OUT}/${theme}-${MODE}.png` })
  if (MODE === 'after') {
    // The pencil editor opens on the typed words only; the envelope stays
    // out of the edit box and is glued back on commit.
    await page.getByLabel('Edit queued message').first().click()
    const editor = page.getByRole('textbox', { name: 'Edit queued message' })
    await editor.waitFor({ state: 'visible', timeout: 5000 })
    const value = await editor.inputValue()
    check(`${theme} editor holds the typed line only`, value === TYPED, JSON.stringify(value))
    check(`${theme} editor keeps the knowledge chip`, await page.getByTestId('queue-card-knowledge').first().isVisible(), 'chip missing')
    await pane.screenshot({ path: `${OUT}/${theme}-editor-after.png` })
  }
  await close()
}

for (const theme of ['light', 'dark']) {
  await capture(theme)
  if (MODE === 'after') { await captureFallback(theme); await captureNarrow(theme) }
}
if (failed()) { console.error('frame assertions failed'); process.exit(1) }
console.log(`all frames verified (${MODE}) →`, OUT)
