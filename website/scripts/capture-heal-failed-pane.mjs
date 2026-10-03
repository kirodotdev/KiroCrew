/**
 * Screenshot harness for PR #15029: the heal-failed notice as a SPLIT-VIEW pane
 * shows it. A pane on screen beside the open chat re-reads itself after a
 * link-access change; when that re-read fails, the pane's own load-error row
 * (`chat-pane-hydrate-error`) carries the heal copy with its Retry beside it.
 * capture-heal-failed-notice.mjs photographs the open chat's banner; this one
 * photographs the narrower in-pane row, which lays the same two sentences out
 * next to the button instead of above it.
 *
 * Runs against the built SPA (website/dist) with every /api/** call stubbed: the
 * second pane's slot reads fail while the change lands, then succeed for Retry.
 *
 * Usage: node scripts/capture-heal-failed-pane.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import {
  TWO_PANE_SPLIT_LAYOUTS, jsonResponder, makeChecker, prepareSplitChatPage, splitPaneFixtures,
} from './lib/prepare-split-chat-page.mjs'

const OUT = process.argv[2] || '../temp-screenshots/15029-heal-failed-notice'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slots = ['pane-a', 'pane-b'].map((key, i) => ({
  key, title: i ? 'Where did the review link go?' : 'Compare the two layout options',
  running: false, last_ts: now - 60 * (i + 1), message_count: 2,
}))
const paneDetail = (q, a) => ({
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', ts: now - 300, content: q, cls: 'msg msg-user', meta: { mid: `${q}-u` } },
    { role: 'assistant', ts: now - 240, content: a, cls: 'msg msg-assistant', meta: { mid: `${q}-a` } },
  ],
})
const detailA = paneDetail('Compare the two layout options.', 'Option A keeps the sidebar fixed.')
const detailB = paneDetail('Can you link me the review for the export fix?', 'The review is up; the link is in the summary above.')

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const { check, failed } = makeChecker()
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 1 })
    let failing = false
    const page = await prepareSplitChatPage(context, {
      base, fixtures: splitPaneFixtures(slots), detailA, detailB,
      splitLayouts: TWO_PANE_SPLIT_LAYOUTS, json: jsonResponder, theme,
      pre: async (path, route) => {
        if (failing && path.startsWith('/api/chat/slots/pane-b')) {
          await jsonResponder(route, { error: 'gateway unavailable' }, 503)
          return true
        }
        return false
      },
    })
    const panes = page.locator('[data-chat-pane]')
    await panes.nth(1).waitFor({ state: 'visible', timeout: 20000 })
    await page.waitForTimeout(2000)
    failing = true
    await page.evaluate(() => {
      window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: 'pane-a', gen: 'g-2' } }))
    })
    const notice = page.getByTestId('chat-pane-hydrate-error')
    await notice.waitFor({ timeout: 10000 })
    const inPaneB = await panes.nth(1).getByTestId('chat-pane-hydrate-error').count()
    check(`${theme}: notice is in the second pane`, inPaneB === 1, `count=${inPaneB}`)
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/heal-failed-pane-${theme}.png` })
    failing = false
    await panes.nth(1).getByText('Retry').click()
    await notice.waitFor({ state: 'detached', timeout: 10000 })
    await context.close()
  }
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}`)
  if (failed()) process.exit(1)
}

main().catch(err => { console.error(err); process.exit(1) })
