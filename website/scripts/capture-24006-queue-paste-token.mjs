/**
 * Screenshot evidence for #24006: a message sent with a collapsed
 * `[ Paste #N ]` token while the agent is busy is queued with its wire text,
 * in which the paste is expanded in place. The queue card used to show the
 * whole pasted text instead of the token the sent bubble shows.
 *
 * Runs the REAL SPA (fixture shape of capture-23632-queue-knowledge-envelope.mjs):
 * a persisted 2-pane split whose background pane's slot detail lists ONE queue
 * entry, in the gateway's `{id, content, meta}` shape, whose content is the
 * wire text a send of the typed line plus one paste produces. The paste side
 * table is seeded the way the composer seeds it on a send (`saveStoredPaste`:
 * the wire text as the key, the display text and blocks as the value), so the
 * entry takes the same route a real queued send takes on this browser. The
 * frame ASSERTS what the card shows:
 *   MODE=after  (default) the card shows the typed line with its token and
 *               none of the pasted lines, and the pencil editor
 *               (`<theme>-editor-after.png`) opens on that same line
 *   MODE=before run against a checkout of the base commit: the card shows the
 *               pasted lines -- the defect
 *
 * Usage (from website/):
 *   node scripts/capture-24006-queue-paste-token.mjs http://127.0.0.1:4173 ../temp-screenshots/24006-queue-paste-token
 *   MODE=before node scripts/capture-24006-queue-paste-token.mjs http://127.0.0.1:4174 ../temp-screenshots/24006-queue-paste-token
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

const BASE = process.argv[2] || 'http://127.0.0.1:4173'
const OUT = process.argv[3] || '../temp-screenshots/24006-queue-paste-token'
const MODE = process.env.MODE === 'before' ? 'before' : 'after'
mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const PASTE = [
  'Traceback (most recent call last):',
  '  File "deploy/rollout.py", line 212, in <module>',
  '    main()',
  '  File "deploy/rollout.py", line 180, in main',
  '    drain_canary(cluster, timeout=600)',
  '  File "deploy/rollout.py", line 97, in drain_canary',
  '    wait_for_idle(pods, deadline)',
  '  File "deploy/rollout.py", line 61, in wait_for_idle',
  '    raise TimeoutError(f"{len(busy)} pods still busy after {deadline}s")',
  'TimeoutError: 3 pods still busy after 600s',
  'canary-7f9c: 2 in-flight requests',
  'canary-7fa1: 1 in-flight request',
].join('\n')
// The legacy token form, with no invisible id run: what the user reads.
const TOKEN = '[ Paste #1 · 12 lines ]'
const TYPED = `This is the failing rollout ${TOKEN} -- why did the drain time out?`
// The wire text the composer sends: the token expanded in place.
const WIRE = TYPED.replace(TOKEN, PASTE)
const BLOCK = { id: 'p-demo-1', seq: 1, lines: 12, content: PASTE }
// The side-table record the composer writes on the send (saveStoredPaste),
// keyed by the wire text with trailing whitespace removed.
const SIDE_TABLE = JSON.stringify({ [WIRE.trimEnd()]: { displayTxt: TYPED, pastes: [BLOCK], savedAt: Date.now(), seq: 1 } })

const splitLayouts = TWO_PANE_SPLIT_LAYOUTS
const { check, failed } = makeChecker()
const visible = (s) => s.replace(/[\u200b\u200c\u2063]/g, '')

async function openPage(theme, width = 1440) {
  const { slots, detailA, detailB } = queuedPaneFixtures(now, WIRE)
  // The gateway's queue shape, so the entry takes the slot-detail hydration
  // route a real queued send takes (not a pre-built transcript row).
  detailB.messages = detailB.messages.filter(m => m.role !== 'queued')
  detailB.queue = [{ id: 'q-demo-1', content: WIRE, meta: { sendId: 's-demo-1' } }]
  const browser = await chromium.launch()
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
  await ctx.addInitScript((table) => { localStorage.setItem('mc-paste-store-v1', table) }, SIDE_TABLE)
  const page = await prepareSplitChatPage(ctx, { base: BASE, fixtures: splitPaneFixtures(slots), detailA, detailB, splitLayouts, json, theme })
  const card = page.getByTestId('queue-card')
  await card.first().waitFor({ state: 'visible', timeout: 15000 })
  const applied = await page.evaluate(() => document.documentElement.dataset.mode)
  check(`${theme} theme applied`, applied === theme, `data-mode=${applied}`)
  const close = async () => { await ctx.close(); await browser.close() }
  return { page, card, close }
}

async function capture(theme) {
  const { page, card, close } = await openPage(theme)
  const text = visible((await card.first().textContent()) || '')
  if (MODE === 'after') {
    check(`${theme} card shows the typed line with its token`, text.includes(TYPED), JSON.stringify(text))
    check(`${theme} card hides the pasted lines`, !text.includes('Traceback') && !text.includes('TimeoutError'), JSON.stringify(text))
  } else {
    check(`${theme} card shows the pasted lines (defect)`, text.includes('Traceback'), JSON.stringify(text))
    check(`${theme} card shows no token (defect)`, !text.includes(TOKEN), JSON.stringify(text))
  }
  // Frame the background pane (the one holding the card) so the composite
  // shows the card and the composer around it, not the whole split.
  const pane = page.locator('[data-chat-pane]').nth(1)
  await pane.screenshot({ path: `${OUT}/${theme}-${MODE}.png` })
  if (MODE === 'after') {
    // The pencil editor opens on the token too; the paste expands again from
    // the row's blocks when the edit commits (QueueStack.test.tsx pins that).
    await page.getByLabel('Edit queued message').first().click()
    const editor = page.getByRole('textbox', { name: 'Edit queued message' })
    await editor.waitFor({ state: 'visible', timeout: 5000 })
    const value = visible(await editor.inputValue())
    check(`${theme} editor holds the typed line with its token`, value === TYPED, JSON.stringify(value))
    await pane.screenshot({ path: `${OUT}/${theme}-editor-after.png` })
  }
  await close()
}

for (const theme of ['light', 'dark']) await capture(theme)
if (failed()) { console.error('frame assertions failed'); process.exit(1) }
console.log(`all frames verified (${MODE}) →`, OUT)
