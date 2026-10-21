/**
 * Screenshot harness for the "Send a copy to" failure row.
 *
 * Runs the REAL built SPA (website/dist) against fixture APIs — no gateway, no
 * credential. The only substitution is the peer: `/send-session` answers with
 * the 502 body the gateway produces when the tunnel manager reports
 * `transfer_peer_too_old`, which is what an instance with no importer route
 * causes.
 *
 * ## What this proves — and what a picture cannot
 *
 * The reason lives in the row's `title` attribute, and a `title` tooltip is
 * painted by the OS, not the page: Chromium never puts it in the screenshot.
 * So the frame is evidence of the FAILURE ROW, and the asserted `title` below
 * is evidence of the MESSAGE. The assertion is the load-bearing half — it fails
 * the harness if the reason regresses to a bare status code, which no amount of
 * re-reading a screenshot would catch.
 *
 * Usage: node scripts/capture-send-to-instance-error.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/transfer-peer-too-old'
const SLOT = 'chat-peerold'
const PROJECT = '/home/user/workspace/KiroCrew'
const INSTANCE = 'devdesk-2'

/** The gateway's 502 body for a peer with no importer route. */
const PEER_TOO_OLD = {
  error: 'instance is running an older Kiro Crew that cannot receive sessions — update it, then reconnect',
  code: 'transfer_peer_too_old',
}

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Send to instance',
  running: false,
  last_message: '',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = { running: false, has_more: false, total: 0, queue: [], project: PROJECT, messages: [] }

const instances = {
  active: true,
  warm_set_cap: 3,
  sso: { required: false, ok: true, detail: '' },
  instances: [{
    id: INSTANCE,
    name: 'devdesk',
    ssh_host: 'dev-dsk.example.com',
    remote_port: 7788,
    local_port: 7788,
    ttl: '20h',
    remote_bin: '',
    connection_method: 'ssh',
    ssm_target: '',
    aws_profile: '',
    aws_region: '',
    ssm_run_as: 'ec2-user',
    was_connected: true,
    status: { instance_id: INSTANCE, state: 'connected', local_port: 7788, detail: '', error: '' },
  }],
}

const { srv, base } = await serveDist()

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(`PAGEERROR: ${e.message}`))
page.on('console', m => {
  // The 502 is the scene, not a defect: the fixture peer refuses on purpose.
  if (m.type() !== 'error' || /status of 502/.test(m.text())) return
  errors.push(m.text().slice(0, 200))
})

await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path.endsWith('/send-session')) return json(route, PEER_TOO_OLD, 502)
  if (path === '/api/instances') return json(route, instances)
  if (path === '/api/chat/slots') return json(route, slots)
  if (path.startsWith('/api/chat/slots/')) return json(route, detail)
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript((slot) => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-active-slot-chat', slot)
}, SLOT)

await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
await page.getByLabel('Message input').waitFor({ timeout: 20000 })

// The session-header dropdown is the surface that carries the submenu.
await page.getByRole('button', { name: /session options/i }).click()
await page.getByRole('menu').first().waitFor({ timeout: 10000 })

await page.getByText(/send a copy to/i).hover()
const row = page.getByRole('menuitem', { name: /devdesk/i })
await row.waitFor({ timeout: 10000 })
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/send-to-instance-menu.png` })

await row.click()

// The row reports the outcome in place — wait for the inline notice to land.
const inline = page.getByRole('alert').first()
await inline.waitFor({ timeout: 10000 })
await page.waitForTimeout(400)

// Assert the reason, don't just photograph the row: a screenshot cannot prove
// the peer's own sentence is what reached the reader rather than a status code.
const reason = (await inline.textContent() || '').trim()
if (!reason.includes('older Kiro Crew')) errors.push(`ASSERT: reason does not name the cause: ${reason}`)
if (!reason.includes('update it')) errors.push(`ASSERT: reason does not name the remedy: ${reason}`)
if (/HTTP \d\d\d/.test(reason)) errors.push(`ASSERT: reason still leaks a status code: ${reason}`)
// The hand-off is a sibling menu item, so it is reachable by keyboard from the row.
const handoff = page.getByRole('menuitem', { name: /ask the agent/i })
if (await handoff.count() === 0) errors.push('ASSERT: the row carries no agent hand-off item')
// While the menu is open the banner must NOT also be showing: two copies of the
// peer sentence and two hand-off buttons is the duplication this scene proves gone.
if (await page.getByTestId('session-action-error').count() > 0) {
  errors.push('ASSERT: the page banner is showing while the row already carries the failure')
}

await page.screenshot({ path: `${OUT}/send-to-instance-failed-dark.png` })
await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
await page.waitForTimeout(500)
await page.screenshot({ path: `${OUT}/send-to-instance-failed-light.png` })
await page.evaluate(() => { document.documentElement.dataset.theme = 'dark' })

// Closing the menu hands the same failure to the page banner, which is the only
// surface that survives it. This is the frame for the main error path.
await page.keyboard.press('Escape')
await page.keyboard.press('Escape')
const banner = page.getByTestId('session-action-error')
await banner.waitFor({ timeout: 10000 })
await page.waitForTimeout(400)
const bannerText = (await banner.textContent() || '').trim()
if (!bannerText.includes('older Kiro Crew')) errors.push(`ASSERT: banner drops the peer reason: ${bannerText}`)
if (!/“|”/.test(bannerText)) errors.push(`ASSERT: banner does not set the peer sentence off in quotes: ${bannerText}`)
if (!/Couldn’t send a copy/.test(bannerText)) errors.push(`ASSERT: banner heading is not the send lead: ${bannerText}`)
if (await page.getByRole('menu').count() > 0) errors.push('ASSERT: the menu is still open, so this is not the handover frame')
await page.screenshot({ path: `${OUT}/send-to-instance-banner-dark.png` })

await context.close()
await browser.close()
srv.close()

console.log(JSON.stringify({ out: OUT, reason, errors }, null, 2))
if (errors.length) process.exitCode = 1
