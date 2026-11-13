/**
 * Screenshots for "remember the docked side panel's open/closed choice per
 * crewmate" (Bots page). Mounts the REAL MembersPage through
 * capture/members-page.html, API route-intercepted (gateway-free), and serves
 * the dev build itself (vite in-process, loopback, ephemeral port).
 *
 * Every frame asserts the state it documents before it is written:
 *   01-oncall-closed     oncall open, its panel closed with the strip's
 *                        "Close panel" control; oncall's own key reads '0'
 *   02-research-open     switched to research: its panel is open (it has no
 *                        choice of its own, so it reads the default), and
 *                        oncall's choice did not carry over
 *   03-oncall-still-closed  back on oncall: its own closed choice again, and
 *                        research's key was never written
 *
 * Usage (in website/):
 *   node scripts/capture-members-panel-per-bot.mjs ../temp-screenshots/members-panel-per-bot
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/members-panel-per-bot')
mkdirSync(OUT, { recursive: true })

const bot = (name, agent, last_message, last_active_ts) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: agent, workspace: 'default', memory_store: 'default', model: '',
  last_active_ts, last_message,
})
const NOW = Math.floor(Date.now() / 1000)
const THREADS = {
  oncall: ['Anything need me today?', 'Nothing urgent. Two pages acknowledged, one runbook updated.'],
  research: ['What did the RFCs say?', 'Summarised the three RFCs you sent: two agree on the cache, one wants a new queue.'],
}
const MEMBERS = [
  bot('oncall', 'kirocrew', THREADS.oncall[1], NOW - 600),
  bot('research', 'kirocrew-research', THREADS.research[1], NOW - 1800),
]
const slotDetail = (name) => ({
  key: `member-${name}`,
  title: name,
  running: false,
  messages: [
    { role: 'user', content: THREADS[name][0], ts: new Date((NOW - 660) * 1000).toISOString() },
    { role: 'assistant', content: THREADS[name][1], ts: new Date((NOW - 600) * 1000).toISOString() },
  ],
  total: 2,
  has_more: false,
  next_before: 0,
})
/** Each crewmate's published dashboard: one card naming it, so a frame shows
 *  whose panel is open. */
const dashboardHtml = (name) => `<div style="padding:16px;font:14px system-ui;color:#ddd">
  <div style="font-size:18px;font-weight:600;margin-bottom:8px">${name} dashboard</div>
  <div style="opacity:.7">Open items: ${name === 'oncall' ? 2 : 5}</div>
</div><script>parent.postMessage({type:'kirocrew-dashboard:ready'},'*')</script>`

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5198, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const failures = []
const check = (name, ok, detail) => {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failures.push(name)
}

const ctx = await browser.newContext({ viewport: { width: 1440, height: 860 }, deviceScaleFactor: 1 })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
await routeMembersApi(page, slotDetail('oncall'), { members: MEMBERS })
// Registered after the shared stub, so these win: each crewmate's own thread
// and dashboard, and an empty team list.
await page.route((u) => /^\/api\/(teams|sandbox-doc|members\/[^/]+\/(panel|dashboard)|chat\/slots\/member-[^/]+)$/.test(new URL(u).pathname), (r) => {
  const path = new URL(r.request().url()).pathname
  const json = (body) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  if (path === '/api/teams') return json({ teams: [] })
  if (path === '/api/sandbox-doc') {
    const { html } = JSON.parse(r.request().postData() || '{}')
    return json({ url: 'data:text/html;base64,' + Buffer.from(String(html || '')).toString('base64') })
  }
  if (/\/panel$/.test(path)) return json({ panel: null, html: null })
  const dash = path.match(/^\/api\/members\/([^/]+)\/dashboard$/)
  if (dash) {
    const html = dashboardHtml(decodeURIComponent(dash[1]))
    return json({ instance_version: 1, template: { id: 'bot-card', version: 1 }, html, rendered_html: html, manifest: { id: 'bot-card', version: 1, fields: {} }, state: 'live', scripts: true })
  }
  return json(slotDetail(decodeURIComponent(path.split('/member-')[1])))
})
await page.goto(`${BASE}/capture/members-page.html?theme=dark&route=${encodeURIComponent('/members?member=oncall')}`)
await page.waitForSelector('[data-capture-root]')

const panel = page.getByTestId('member-side-panel')
const opener = page.getByTestId('member-panel-toggle')
const pill = page.getByTestId('member-identity-pill')
const key = (name) => page.evaluate((n) => localStorage.getItem(`mc-members-panel-open:${n}`), name)
const switchTo = async (name) => {
  await page.getByTestId('crewmate-switcher').click()
  await page.getByTestId('crewmate-switcher-row').filter({ hasText: name }).first().click()
  await page.waitForFunction((n) => document.querySelector('[data-testid="member-identity-pill"]')?.textContent?.includes(n), name)
}
const settle = async () => { await page.mouse.move(700, 500); await page.waitForTimeout(900) }

// 01 — oncall: close the docked panel with its own close control.
await pill.waitFor()
await panel.waitFor()
await panel.getByRole('button', { name: 'Close panel' }).click()
await opener.waitFor()
await panel.waitFor({ state: 'detached' })
await settle()
check('01 pill names oncall', /oncall/.test((await pill.textContent()) || ''), '')
check('01 oncall panel closed', (await panel.count()) === 0, `panels=${await panel.count()}`)
check('01 oncall key written', (await key('oncall')) === '0', `oncall=${await key('oncall')}`)
await page.screenshot({ path: resolve(OUT, '01-oncall-closed-dark.png') })

// 02 — research: its own (default) choice, not oncall's.
await switchTo('research')
await panel.waitFor()
await settle()
check('02 research panel open', (await panel.count()) === 1, `panels=${await panel.count()}`)
check('02 research opener hidden', (await opener.count()) === 0, `openers=${await opener.count()}`)
check('02 research key untouched', (await key('research')) === null, `research=${await key('research')}`)
await page.frameLocator('[data-testid="crew-dashboard-iframe"]').getByText('research dashboard').waitFor({ timeout: 15000 })
await page.screenshot({ path: resolve(OUT, '02-research-open-dark.png') })

// 03 — back to oncall: still closed.
await switchTo('oncall')
await opener.waitFor()
await settle()
check('03 oncall panel still closed', (await panel.count()) === 0, `panels=${await panel.count()}`)
check('03 research key still untouched', (await key('research')) === null, `research=${await key('research')}`)
const asks = await page.getByText(THREADS.oncall[0], { exact: true }).count()
check('03 oncall thread shown once', asks === 1, `copies=${asks}`)
await page.screenshot({ path: resolve(OUT, '03-oncall-still-closed-dark.png') })

check('no page errors', errors.length === 0, errors.join(' | '))
await browser.close()
await server.close()
if (failures.length) {
  console.error(`CAPTURE FAILED: ${failures.length} frame(s) did not match their asserted state: ${failures.join(', ')}`)
  process.exit(1)
}
console.log('all frames verified')
