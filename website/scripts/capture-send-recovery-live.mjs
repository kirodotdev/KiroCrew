/**
 * Live-surface evidence for the persisted send recovery: the REAL app, a stalled send, a page
 * reload while delivery is still in doubt, and the text coming back afterwards.
 *
 * Runs the built SPA (website/dist) with every /api/** call answered from fixtures and storage kept
 * across the reload. `POST /api/chat` is never answered, so the client's 10s deadline raises the
 * delivery warning; the reload then destroys the page that held it in memory.
 *
 * Steps, each asserted before its shot so a wrong state exits non-zero:
 *   01 in-doubt    strip above the composer, composer empty
 *   02 recovered   after the reload: the text is back in the composer, with the recovered notice
 *   03 delivered   the gateway echoes that send after all; the recovered-delivered notice appears
 *
 * Usage: node scripts/capture-send-recovery-live.mjs <outDir> [dark|light]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/send-recovery-live'
const THEME = process.argv[3] === 'light' ? 'light' : 'dark'
mkdirSync(OUT, { recursive: true })
const { srv, base: BASE } = await serveDist()

const MESSAGE = 'Rebase the release branch onto main and rerun the checks'
const RECOVERED = 'A message from before the page reloaded was never confirmed'
const DELIVERED = 'A message from before the page reloaded was delivered after all.'
const now = Date.now() / 1000

let socket = null
let sent = null
const holdChat = async (path, route) => {
  if (path !== '/api/chat' || route.request().method() !== 'POST') return false
  sent = JSON.parse(route.request().postData() || '{}')
  return true
}

const fail = msg => { console.error('CAPTURE FAILED:', msg); process.exit(1) }

const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '' } })
const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2, colorScheme: THEME })
const page = await context.newPage()
page.on('pageerror', e => console.log('PAGEERROR:', String(e).slice(0, 200)))
await stubDashboardApi(page, {
  theme: THEME,
  slots: [{ key: 'chat-1', title: 'Release prep', messages: 0, running: false, agent: 'kirocrew', mode: '' }],
  extra: holdChat,
  preserveStorage: true,
  localStorageEntries: { 'mc-active-slot': 'chat-1', 'mc-lang': 'en' },
})
await page.routeWebSocket(/\/api\/ws/, ws => { socket = ws })
await page.goto(BASE + '/chat', { waitUntil: 'domcontentloaded' })

const shot = async name => {
  const p = join(OUT, `page-${THEME}-${name}.png`)
  await page.screenshot({ path: p })
  console.log('wrote', p)
}
const composer = () => page.getByLabel('Message input').first()

await composer().waitFor({ state: 'visible', timeout: 20000 }).catch(() => fail('composer never became visible'))
await composer().fill(MESSAGE)
await composer().press('Enter')
const exit = page.getByTestId('delivery-exit-dismiss')
await exit.waitFor({ state: 'visible', timeout: 20000 }).catch(() => fail('strip never appeared after the deadline'))
const sendId = sent?.meta?.sendId
if (!sendId) fail('no POST /api/chat with a send id was observed')
if ((await composer().inputValue()).includes(MESSAGE)) fail('composer already holds the text before the reload')
await page.waitForTimeout(600)
await shot('01-in-doubt')

socket = null
await page.reload({ waitUntil: 'domcontentloaded' })
await page.getByText(RECOVERED).first().waitFor({ state: 'visible', timeout: 20000 })
  .catch(() => fail('recovered notice never appeared after the reload'))
await page.waitForTimeout(600)
if (!(await composer().inputValue()).includes(MESSAGE)) fail('text not back in the composer after the reload')
if ((await page.getByText(RECOVERED).count()) !== 1) fail('recovered notice is not exactly one')
await shot('02-recovered')

if (!socket) fail('no websocket was opened after the reload')
socket.send(JSON.stringify({
  type: 'chat_message',
  data: { slot: 'chat-1', role: 'user', content: MESSAGE, cls: 'msg msg-u', ts: now + 30, meta: { sendId, mid: 'm-late-echo' } },
}))
await page.getByText(DELIVERED).first().waitFor({ state: 'visible', timeout: 10000 })
  .catch(() => fail('recovered-delivered notice never appeared'))
await page.waitForTimeout(600)
if ((await page.getByText(DELIVERED).count()) !== 1) fail('recovered-delivered notice is not exactly one')
await shot('03-delivered-after-all')

await context.close()
await browser.close()
srv.close()
console.log(JSON.stringify({ theme: THEME, sendId, ok: true }))
