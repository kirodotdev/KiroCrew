/**
 * Capture Crew Mode UI evidence — gateway-free (stubbed dashboard API).
 *
 * Shots:
 *  1. new-dropdown.png    — the New split-button dropdown open, showing the
 *                           "New Crew Mode chat" item under Autopilot.
 *  2. crew-session.png    — a crew-mode session with an interleaved
 *                           multi-topic transcript (acks + attributed
 *                           forwards) and the Crew badge in the sidebar row.
 *
 * Usage: node scripts/capture-crew-mode.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-mode'
mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const msg = (role, content, i) => ({
  role,
  content,
  cls: role === 'user' ? 'msg msg-u' : 'msg msg-a',
  ts: new Date((now - 600 + i * 30) * 1000).toISOString(),
})

const CREW_MESSAGES = [
  msg('user', 'check why the stable feed returns 403', 0),
  msg('assistant', 'On it.', 1),
  msg('user', 'also write me a script that tails the gateway log with colors', 2),
  msg('assistant', 'Got it — working on that.', 3),
  msg('user', 'and can you explain how our TTL sweep works?', 4),
  msg('assistant', 'Picking that up now.', 5),
  msg('assistant', '↩ re: “check why the stable feed returns 403”\n\nRoot cause found: v0.1.1 predates the yml feed migration, so `latest-mac.yml` was never published for the stable channel; the 403 (vs 404) is CloudFront+OAC missing `s3:ListBucket`. Three fix options ready when you are.', 6),
  msg('assistant', '↩ re: “also write me a script that tails the gateway log with colors”\n\nDone — `scripts/tail-gateway.sh` colorizes level tokens (ERROR red, WARN yellow) and follows rotation. Tested against a live log; handles gaps on restart.', 7),
]

const SLOTS = [
  { key: 'crew-demo', title: 'Multi-topic afternoon', agent: 'kirocrew', mode: 'crew', surface: 'crew', running: false, unread: 0, pinned: false, memory_mode: 'persistent', messages: 8, last_ts: new Date().toISOString(), created: new Date(Date.now() - 3600e3).toISOString() },
  { key: 'plain-1', title: 'Fix stable feed', agent: 'kirocrew', mode: '', surface: '', running: false, unread: 0, pinned: false, memory_mode: 'persistent', messages: 0, last_ts: new Date(Date.now() - 600e3).toISOString(), created: new Date(Date.now() - 7200e3).toISOString() },
]

const extra = async (path, route) => {
  if (path === '/api/chat/slots') return json(route, SLOTS), true
  if (path === '/api/chat/slots/crew-demo')
    return json(route, { ...SLOTS[0], messages: CREW_MESSAGES }), true
  if (path === '/api/chat/slots/plain-1')
    return json(route, { ...SLOTS[1], messages: [] }), true
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
logPageProblems(page)
await stubDashboardApi(page, { extra })

await page.goto(base, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1200)

// Shot 1: open the New split-button dropdown
await page.getByLabel('More create options').first().click()
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/new-dropdown.png` })

// Shot 2: crew session transcript + sidebar badge
await page.keyboard.press('Escape')
await page.waitForTimeout(200)
await page.screenshot({ path: `${OUT}/debug-sidebar.png` })
await page.locator('text=Multi-topic afternoon').first().click({ timeout: 8000 })
await page.waitForTimeout(900)
await page.screenshot({ path: `${OUT}/crew-session.png` })

console.log(`WROTE ${OUT}/new-dropdown.png`)
console.log(`WROTE ${OUT}/crew-session.png`)
await browser.close()
srv.close()
