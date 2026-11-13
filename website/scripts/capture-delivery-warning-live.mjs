/**
 * Live-surface evidence for the delivery-warning strip: the REAL app, a stalled send, the
 * strip in the real composer, the exit, and the late delivery that follows it.
 *
 * Runs the built SPA (website/dist) with every /api/** call answered from fixtures and the /api/ws socket held
 * open by Playwright, so the harness can deliver a frame the way the gateway would. The one
 * scene-specific stub is `POST /api/chat`: it is never answered, so the client's own 10s abort
 * deadline fires and the send becomes `response-late`, the only path that raises the strip.
 *
 * Steps, each asserted before its shot so a wrong state exits non-zero instead of producing
 * a misleading image:
 *   01 in-doubt   strip above the composer, the pending row, and the reworded notice row
 *   02 removed    row gone, text back in the composer, strip gone
 *   03 delivered  the gateway echoes the removed send; the delivered-after-all notice appears
 *
 * `surface` is `page` (the full chat page) or `pane` (a split-view ChatPane).
 *
 * Usage: node scripts/capture-delivery-warning-live.mjs <outDir> <page|pane> [dark|light]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { prepareSplitChatPage, TWO_PANE_SPLIT_LAYOUTS, jsonResponder, splitPaneFixtures } from './lib/prepare-split-chat-page.mjs'

const OUT = process.argv[2] || '../temp-screenshots/delivery-warning-live'
const SURFACE = process.argv[3] === 'pane' ? 'pane' : 'page'
const THEME = process.argv[4] === 'light' ? 'light' : 'dark'
mkdirSync(OUT, { recursive: true })
const { srv, base: BASE } = await serveDist()

const MESSAGE = 'Rebase the release branch onto main and rerun the checks'
const IN_DOUBT = 'Delivery not confirmed yet.'
const LATE = 'A message you removed was delivered after all.'
const now = Date.now() / 1000

let socket = null
let sent = null
const holdChat = async (path, route) => {
  if (path !== '/api/chat' || route.request().method() !== 'POST') return false
  sent = JSON.parse(route.request().postData() || '{}')
  return true
}

async function openPage(context) {
  const page = await context.newPage()
  await stubDashboardApi(page, {
    theme: THEME,
    slots: [{ key: 'chat-1', title: 'Release prep', messages: 0, running: false, agent: 'kirocrew', mode: '' }],
    extra: holdChat,
    localStorageEntries: { 'mc-active-slot': 'chat-1', 'mc-lang': 'en' },
  })
  await page.routeWebSocket(/\/api\/ws/, ws => { socket = ws })
  await page.goto(BASE + '/chat', { waitUntil: 'domcontentloaded' })
  return { page, slot: 'chat-1', scope: page }
}

async function openPane(context) {
  const slots = ['pane-a', 'pane-b'].map((key, i) => ({
    key, title: i ? 'Release prep' : 'Design notes', running: false, messages: 1,
    agent: 'kirocrew', memory_mode: 'persistent', modified: Math.floor(now) - i * 60,
  }))
  const detail = text => ({
    running: false, has_more: false, total: 1, queue: [],
    messages: [{ role: 'assistant', ts: now - 120, content: text, cls: 'msg msg-assistant' }],
  })
  const page = await prepareSplitChatPage(context, {
    base: BASE, fixtures: { ...splitPaneFixtures(slots), '/api/lang': { lang: 'en' } },
    detailA: detail('Option A keeps the sidebar fixed.'), detailB: detail('Ready for the release checks.'),
    splitLayouts: TWO_PANE_SPLIT_LAYOUTS, json: jsonResponder, pre: holdChat, theme: THEME,
  })
  await page.routeWebSocket(/\/api\/ws/, ws => { socket = ws })
  await page.reload({ waitUntil: 'domcontentloaded' })
  return { page, slot: 'pane-b', scope: page.locator('[data-chat-pane]').nth(1) }
}

const fail = msg => { console.error('CAPTURE FAILED:', msg); process.exit(1) }

const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '' } })
const context = await browser.newContext({
  viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2, colorScheme: THEME,
  recordVideo: { dir: join(OUT, 'video'), size: { width: 1280, height: 820 } },
})
const { page, slot, scope } = SURFACE === 'pane' ? await openPane(context) : await openPage(context)
page.on('pageerror', e => console.log('PAGEERROR:', String(e).slice(0, 200)))

const composer = scope.getByLabel('Message input').first()
await composer.waitFor({ state: 'visible', timeout: 20000 }).catch(async () => {
  await page.screenshot({ path: join(OUT, `${SURFACE}-${THEME}-debug.png`) })
  fail('composer never became visible')
})
await composer.fill(MESSAGE)
await composer.press('Enter')

const exit = scope.getByTestId('delivery-exit-dismiss')
await exit.waitFor({ state: 'visible', timeout: 20000 }).catch(() => fail('strip never appeared after the deadline'))
if (!sent) fail('no POST /api/chat was observed')
const sendId = sent.meta?.sendId ?? sent.meta?.send_id
if (!sendId) fail(`POST meta has no send id; keys: ${Object.keys(sent.meta || {}).join(',')}`)
const rows = () => scope.locator('.message-bubble', { hasText: MESSAGE }).count()
if ((await rows()) < 1) fail('pending row missing')
if ((await scope.getByText(IN_DOUBT).count()) < 1) fail('in-doubt notice row missing')
if ((await composer.inputValue()).includes(MESSAGE)) fail('composer already holds the text before Remove')
const shot = async name => {
  const p = join(OUT, `${SURFACE}-${THEME}-${name}.png`)
  await page.screenshot({ path: p })
  console.log('wrote', p)
}
await page.waitForTimeout(600)
await shot('01-in-doubt')

await exit.click()
await page.waitForTimeout(600)
if ((await rows()) !== 0) fail('row still present after Remove')
if (!(await composer.inputValue()).includes(MESSAGE)) fail('text not back in the composer')
if ((await exit.count()) !== 0) fail('strip still visible after Remove')
await shot('02-removed')

if (!socket) fail('no websocket was opened')
socket.send(JSON.stringify({
  type: 'chat_message',
  data: { slot, role: 'user', content: MESSAGE, cls: 'msg msg-u', ts: now + 30, meta: { sendId, mid: 'm-late-echo' } },
}))
await scope.getByText(LATE).first().waitFor({ state: 'visible', timeout: 10000 })
  .catch(() => fail('delivered-after-all notice never appeared'))
await page.waitForTimeout(600)
if ((await scope.getByText(LATE).count()) !== 1) fail('delivered-after-all notice is not exactly one')
await shot('03-delivered-after-all')

const video = page.video()
await context.close()
if (video) await video.saveAs(join(OUT, `${SURFACE}-${THEME}-recording.webm`))
await browser.close()
srv.close()
console.log(JSON.stringify({ surface: SURFACE, theme: THEME, sendId, ok: true }))
