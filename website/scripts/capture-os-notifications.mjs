/**
 * Screenshot harness for the OS notification surface (#3521).
 *
 * Runs the REAL built SPA (website/dist) gateway-free (stubDashboardApi).
 * A real OS banner cannot be photographed from the page, so the page's
 * `Notification` global is replaced BEFORE the app boots with a recorder
 * that renders each construction into a fixed-position proof card — the
 * captured content (title, body, tag) is exactly what the OS would show.
 *
 * Frames:
 *   01-settings-os-section   the new "OS notifications" Settings section:
 *                            master toggle, per-category toggles, Test button
 *   02-banner-proof          proof card for a bus note delivered while the
 *                            tab is hidden: note title/body forwarded as-is
 *                            (no "N new notifications"), stable tag
 *   03-approval-single-banner proof card for an approval frame: exactly ONE
 *                            banner (the historical duplicate is gone),
 *                            session name resolved from the owning slot
 *
 * Usage: node scripts/capture-os-notifications.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/os-notifications'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
logPageProblems(page)

// Replace Notification BEFORE any app code runs: a recorder whose every
// construction appends a proof card to a fixed overlay. Permission reports
// 'granted' so the surface is live.
await page.addInitScript(() => {
  const constructed = []
  class RecordingNotification {
    static permission = 'granted'
    static requestPermission = async () => 'granted'
    constructor(title, options = {}) {
      constructed.push({ title, options })
      const host = document.getElementById('os-notif-proof') || (() => {
        const d = document.createElement('div')
        d.id = 'os-notif-proof'
        d.style.cssText = 'position:fixed;top:16px;right:16px;z-index:99999;display:flex;flex-direction:column;gap:8px;font-family:-apple-system,sans-serif'
        document.body.appendChild(d)
        return d
      })()
      const card = document.createElement('div')
      card.style.cssText = 'background:#2b2b2e;color:#f5f5f7;border-radius:12px;padding:12px 16px;width:360px;box-shadow:0 8px 24px rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.12)'
      card.innerHTML = `
        <div style="font-size:11px;opacity:.55;margin-bottom:4px">OS NOTIFICATION (recorded new Notification())</div>
        <div style="font-weight:600;font-size:13px">${title}</div>
        <div style="font-size:12px;opacity:.85;margin-top:2px">${options.body || ''}</div>
        <div style="font-size:10px;opacity:.45;margin-top:6px">tag: ${options.tag || ''}</div>`
      host.appendChild(card)
    }
    close() { /* recorded only */ }
  }
  Object.defineProperty(window, 'Notification', { value: RecordingNotification, configurable: true })
  window.__osNotifConstructed = constructed
})

let wsServer = null
await stubDashboardApi(page, {
  extra: async (path, route) => {
    if (path === '/api/notifications') {
      await json(route, { notifications: [], unread: 0 })
      return true
    }
    if (path === '/api/chat/slots' && route.request().method() === 'POST') {
      await json(route, { key: 'chat-1', name: 'chat-1', title: 'Refactor billing pipeline', messages: [], running: false })
      return true
    }
    if (path === '/api/chat/slots') {
      await json(route, [{ key: 'chat-1', title: 'Refactor billing pipeline', messages: 3, running: true }])
      return true
    }
    return false
  },
})
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

// ---- Frame 1: the Settings section ----------------------------------------
await page.goto(base + '/settings?tab=notifications')
const osHeader = page.getByText('OS notifications', { exact: true }).first()
await osHeader.waitFor({ state: 'visible', timeout: 15000 })
await osHeader.scrollIntoViewIfNeeded()
await page.waitForTimeout(600) // settle stagger animation
await page.screenshot({ path: `${OUT}/01-settings-os-section.png` })

// ---- Frame 2: bus note forwarded as-is while hidden ------------------------
// The surface only fires when document.hidden — shadow the getter.
await page.evaluate(() => {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
})
await page.evaluate(() => {
  window.dispatchEvent(new CustomEvent('mc-notification', {
    detail: {
      kind: 'cron',
      title: 'Nightly backup finished',
      body: 'Completed in 42s — 3 artifacts uploaded',
      tag: 'job-nightly-backup',
    },
  }))
})
await page.waitForSelector('#os-notif-proof', { timeout: 5000 })
await page.screenshot({ path: `${OUT}/02-banner-proof.png` })

// ---- Frame 3: approval => exactly one banner, session name in title --------
await page.evaluate(() => { document.getElementById('os-notif-proof')?.remove() })
if (!wsServer) throw new Error('WS never connected')
wsServer.send(JSON.stringify({
  type: 'approval',
  data: { id: 'ap-77', slot: 'chat-1', source: 'agent', tool: 'execute_bash', tool_input: '{"command":"npm test"}', ts: 7.0 },
}))
await page.waitForSelector('#os-notif-proof', { timeout: 5000 })
const count = await page.evaluate(() => window.__osNotifConstructed.filter(c => (c.options.tag || '').startsWith('kirocrew-approval')).length)
if (count !== 1) throw new Error(`expected exactly 1 approval banner, got ${count}`)
await page.screenshot({ path: `${OUT}/03-approval-single-banner.png` })

await browser.close()
srv.close()
console.log(`captured 3 frames -> ${OUT}`)
