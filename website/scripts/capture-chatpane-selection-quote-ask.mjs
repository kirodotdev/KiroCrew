/**
 * Screenshot harness for Quote / Ask in a SPLIT-VIEW pane (ChatPane inside
 * SessionGridView), the half of the selection-actions feature the Members
 * page frames cannot show.
 *
 * Runs the REAL SPA with every /api/** call and the /api/ws websocket
 * intercepted by Playwright and answered from fixtures — no gateway. A
 * persisted 2-pane split layout anchored at the active slot (pane-a)
 * auto-enters split mode on load. The selection is made in pane-b, the
 * NON-active pane: that is the case the slot-named seed and the
 * re-bind-the-panel step exist for.
 *
 * Captures (each state-asserted before the frame):
 *   1. split-toolbar:  triple-click selection in pane-b → Quote / Ask in
 *                      Side Chat / Copy over the reply
 *   2. split-ask:      Ask re-bound the page to pane-b (its title now heads
 *                      the activity panel), opened the Side tab, and seeded
 *                      pane-b's Side Chat with the selection; both pane
 *                      composers stay empty
 *   3. split-quote:    Quote landed in pane-b's OWN composer, pane-a's empty
 *
 * Usage: node scripts/capture-chatpane-selection-quote-ask.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import {
  TWO_PANE_SPLIT_LAYOUTS,
  jsonResponder as json,
  makeChecker,
  prepareSplitChatPage,
  splitPaneFixtures,
} from './lib/prepare-split-chat-page.mjs'
import { mkdirSync } from 'node:fs'

delete process.env.LD_LIBRARY_PATH

const BASE = process.argv[2] || 'http://127.0.0.1:3000'
const OUT = process.argv[3] || '../temp-screenshots/chatpane-selection-quote-ask'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const REPLY_B = 'Option B collapses the sidebar under 900px and keeps the composer full width.'

const slots = [
  { key: 'pane-a', title: 'Design notes', running: false, last_message: 'Working through phase one…', messages: 2, agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) },
  { key: 'pane-b', title: 'Release checklist', running: false, last_message: 'Summarized the layout options.', messages: 2, agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) - 60 },
]

const detailA = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 300, content: 'Compare the two layout options.', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 240, content: 'Option A keeps the sidebar fixed at every width.', cls: 'msg msg-assistant' },
  ],
}
const detailB = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 120, content: 'And option B?', cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 60, content: REPLY_B, cls: 'msg msg-assistant' },
  ],
}

const splitLayouts = TWO_PANE_SPLIT_LAYOUTS
const FIXTURES = splitPaneFixtures(slots)
const { check, failed } = makeChecker()

const paneComposer = (page, slot) => page.locator(`[data-chat-pane] textarea[data-composer-input]`).nth(slot === 'pane-a' ? 0 : 1)

// One browser context per frame: the app persists the active slot and the
// split layout, so a frame that re-binds the page (Ask) would leak into the
// next frame's boot and land it in single chat.
async function openSplit(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1 })
  const page = await prepareSplitChatPage(ctx, { base: BASE, fixtures: FIXTURES, detailA, detailB, splitLayouts, json })
  await page.getByText(REPLY_B).waitFor({ state: 'visible', timeout: 15000 })
  const panes = await page.locator('[data-chat-pane]').count()
  check('split has two panes', panes === 2, `panes=${panes}`)
  return page
}

async function selectReplyB(page) {
  await page.getByText(REPLY_B).click({ clickCount: 3, position: { x: 40, y: 8 } })
  await page.getByRole('button', { name: 'Ask about this', exact: true }).waitFor({ state: 'visible', timeout: 5000 })
  // The toolbar mounts through a 150ms opacity/scale animation; a frame taken
  // the moment it is "visible" catches it semi-transparent over the selection.
  await page.waitForTimeout(300)
}

async function main() {
  const browser = await chromium.launch()

  // ---- 1. toolbar in the NON-active pane ----
  {
    const page = await openSplit(browser)
    await selectReplyB(page)
    const toolbar = page.getByRole('button', { name: 'Ask about this', exact: true }).locator('xpath=ancestor::div[contains(@class,"z-[9999]")]')
    const labels = (await toolbar.getByRole('button').allTextContents()).map(s => s.trim())
    check('1-split-toolbar three actions', labels.join(' / ') === 'Quote / Ask about this / Copy', `buttons=${labels.join(' / ')}`)
    await page.screenshot({ path: `${OUT}/1-split-toolbar.png` })
    await page.context().close()
  }

  // ---- 2. Ask from pane-b: panel re-bound to pane-b, Side tab open, seeded ----
  {
    const page = await openSplit(browser)
    await selectReplyB(page)
    await page.getByRole('button', { name: 'Ask about this', exact: true }).click()
    const sideSel = '[data-side-chat-input][data-side-chat-slot="pane-b"] textarea[data-composer-input]'
    await page.locator(sideSel).waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForFunction((sel) => (document.querySelector(sel)?.value || '').startsWith('> '), sideSel, { timeout: 5000 })
    const seeded = await page.locator(sideSel).inputValue()
    check('2-split-ask side chat bound to pane-b and seeded', seeded.includes('> Option B collapses'), `side draft=${JSON.stringify(seeded.slice(0, 40))}`)
    // The activity panel is bound to the active slot, so a Side Chat carrying
    // pane-b's marker IS the re-bind; the panel header names pane-b's session.
    const boundPanels = await page.locator('[data-side-chat-input][data-side-chat-slot="pane-b"]').count()
    check('2-split-ask panel re-bound to pane-b', boundPanels === 1, `pane-b side composers=${boundPanels}`)
    check('2-split-ask pane composers untouched', (await paneComposer(page, 'pane-a').inputValue()) === '' && (await paneComposer(page, 'pane-b').inputValue()) === '', 'both pane drafts empty')
    const stillSplit = await page.locator('[data-chat-pane]').count()
    check('2-split-ask split mode kept', stillSplit === 2, `panes=${stillSplit}`)
    await page.screenshot({ path: `${OUT}/2-split-ask-side-chat-seeded.png` })
    await page.context().close()
  }

  // ---- 3. Quote from pane-b lands in pane-b's composer only ----
  {
    const page = await openSplit(browser)
    await selectReplyB(page)
    await page.getByRole('button', { name: 'Quote', exact: true }).click()
    await page.waitForFunction(() => {
      const tas = document.querySelectorAll('[data-chat-pane] textarea[data-composer-input]')
      return (tas[1]?.value || '').startsWith('> ')
    }, undefined, { timeout: 5000 })
    const b = await paneComposer(page, 'pane-b').inputValue()
    const a = await paneComposer(page, 'pane-a').inputValue()
    check('3-split-quote pane-b composer quoted', b.includes('> Option B collapses'), `pane-b draft=${JSON.stringify(b.slice(0, 40))}`)
    check('3-split-quote pane-a untouched', a === '', `pane-a draft=${JSON.stringify(a)}`)
    await page.waitForTimeout(900)
    await page.screenshot({ path: `${OUT}/3-split-quote-in-pane-composer.png` })
    await page.context().close()
  }

  await browser.close()
  if (failed()) { console.error('CAPTURE FAILED: at least one frame did not match its asserted state'); process.exit(1) }
  console.log('all frames verified →', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
