/**
 * Capture Crew Mode UI evidence — gateway-free (stubbed dashboard API).
 *
 * Shots:
 *  1. new-dropdown.png    — the New split-button dropdown open, showing the
 *                           "New Crew Mode chat" item under Autopilot.
 *  2. crew-session.png    — a crew-mode session rendered through the stubbed
 *                           dashboard API. The transcript CONTENT is
 *                           transcribed verbatim from a real driven run of the
 *                           orchestrator pipeline rather than invented, but the
 *                           image itself is a UI capture, not the output of a
 *                           live run: interleaved multi-topic transcript (acks
 *                           plus attributed forwards) and the Crew badge in the
 *                           sidebar row.
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

const CREW_MESSAGES = [
  { role: "user", content: "check why the stable feed returns 403", cls: "msg msg-u", ts: new Date((now - 600 + 0 * 30) * 1000).toISOString() },
  { role: "assistant", content: "On it.", cls: "msg msg-a", ts: new Date((now - 600 + 1 * 30) * 1000).toISOString() },
  { role: "user", content: "also write me a script that tails the gateway log with colors", cls: "msg msg-u", ts: new Date((now - 600 + 2 * 30) * 1000).toISOString() },
  { role: "assistant", content: "Got it — working on that.", cls: "msg msg-a", ts: new Date((now - 600 + 3 * 30) * 1000).toISOString() },
  { role: "user", content: "and can you explain how our TTL sweep works?", cls: "msg msg-u", ts: new Date((now - 600 + 4 * 30) * 1000).toISOString() },
  { role: "assistant", content: "Picking that up now.", cls: "msg msg-a", ts: new Date((now - 600 + 5 * 30) * 1000).toISOString() },
  { role: "user", content: "for the log script, prefer plain grep over awk", cls: "msg msg-u", ts: new Date((now - 600 + 6 * 30) * 1000).toISOString() },
  { role: "assistant", content: "On it.", cls: "msg msg-a", ts: new Date((now - 600 + 7 * 30) * 1000).toISOString() },
  { role: "assistant", content: "↩ re: “also write me a script that tails the gateway log with colors”\n\nDone — `scripts/tail-gateway.sh` colorizes level tokens (ERROR red, WARN yellow) and follows rotation. Tested against a live log; handles gaps on restart.", cls: "msg msg-a crew-reply", ts: new Date((now - 600 + 8 * 30) * 1000).toISOString() },
  { role: "user", content: "what's in flight?", cls: "msg msg-u", ts: new Date((now - 600 + 9 * 30) * 1000).toISOString() },
  { role: "assistant", content: "Got it — working on that.", cls: "msg msg-a", ts: new Date((now - 600 + 10 * 30) * 1000).toISOString() },
  { role: "assistant", content: "Here's what's in flight:\n- **Stable feed 403** — running: just started\n- **Log tail script** — idle: Done — `scripts/tail-gateway.sh` colorizes level tokens (ERROR red, WARN yellow) and follows rotation. Tested against a live log; handles gaps on restart.\n- **TTL sweep explainer** — running: just started", cls: "msg msg-a crew-reply", ts: new Date((now - 600 + 11 * 30) * 1000).toISOString() },
  { role: "assistant", content: "↩ re: “check why the stable feed returns 403”\n\nRoot cause: the CDN origin rejects the stale signing key. Retried with token=[REDACTED: credential] and it returned 200.", cls: "msg msg-a crew-reply", ts: new Date((now - 600 + 12 * 30) * 1000).toISOString() },
  { role: "assistant", content: "↩ re: “and can you explain how our TTL sweep works?”\n\nThe TTL sweep runs every 6h: it scans expiry buckets, drops entries past their deadline, then compacts. Skips the compaction when nothing expired.", cls: "msg msg-a crew-reply", ts: new Date((now - 600 + 13 * 30) * 1000).toISOString() },
  { role: "assistant", content: "↩ re: “also write me a script that tails the gateway log with colors”\n\nSwitched to plain grep as you asked — dropped the awk pass, same colors, one fewer dependency.", cls: "msg msg-a crew-reply", ts: new Date((now - 600 + 14 * 30) * 1000).toISOString() },
]

const SLOTS = [
  { key: 'crew-demo', title: 'Multi-topic afternoon', agent: 'kirocrew', mode: 'crew', surface: 'crew', running: false, unread: 0, pinned: false, memory_mode: 'persistent', messages: 15, last_ts: new Date().toISOString(), created: new Date(Date.now() - 3600e3).toISOString() },
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
