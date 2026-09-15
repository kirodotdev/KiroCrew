/**
 * Screenshot evidence for #9868 — MCP App side-panel tab chips must carry the
 * app's own identity (`server/tool` from the render payload), not the constant
 * "MCP App" label, so two open apps are distinguishable.
 *
 * Drives the REAL built SPA (website/dist) behind the shared `serveDist`
 * server with /api/** stubbed: dashboard config answers `mcp_app_panel: true`,
 * then two `mcp_app_render` frames from DIFFERENT servers are pushed over the
 * routed websocket. The auto-open effect in ChatPage opens one app tab per
 * render; the capture shows both chips side by side with distinct titles.
 *
 * Usage: node scripts/capture-mcp-app-tab-title.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mcp-app-tab-title'
mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-apps'

const slots = [{
  key: SLOT,
  title: 'Two MCP apps open',
  running: false,
  last_message: 'Rendered two apps',
  messages: 1,
  agent: 'kirocrew',
  memory_mode: 'persistent',
}]

const appHtml = (label) =>
  `<!doctype html><html><body style="font-family:sans-serif;padding:16px">${label}</body></html>`

const renderPayload = (toolCallId, server, tool) => ({
  session_key: SLOT,
  tool_call_id: toolCallId,
  server,
  tool,
  html: appHtml(`${server} / ${tool}`),
  csp: null,
  permissions: null,
  spool_id: `spool-${toolCallId}`,
})

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })
const page = await context.newPage()
logPageProblems(page)

let wsServer = null
await stubDashboardApi(page, {
  slots,
  theme: 'dark',
  extra: async (path, route) => {
    if (path === '/api/dashboard/config') {
      json(route, {
        restore_sessions: false, restore_window_minutes: 30,
        merge_queued_messages: false, widget_density: 'more',
        social_share_enabled: true,
        mcp_app_panel: true,
      })
      return true
    }
    return false
  },
})
// AFTER the shared stub so this wins: the stub swallows /api/ws, but this
// harness needs the socket to push the mcp_app_render frames.
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
await page.addInitScript(slot => {
  localStorage.setItem('mc-active-slot', slot)
  localStorage.removeItem('kirocrew.tips.lastShownAt')
}, SLOT)

await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2500)

if (!wsServer) throw new Error('websocket route never bound')
wsServer.send(JSON.stringify({ type: 'mcp_app_render', data: renderPayload('call-1', 'show-tasks', 'open_tasks') }))
await page.waitForTimeout(800)
wsServer.send(JSON.stringify({ type: 'mcp_app_render', data: renderPayload('call-2', 'excalidraw', 'create_view') }))
await page.waitForTimeout(1500)

// Both chips must exist and be distinguishable — fail loudly, never capture a blank.
const chip1 = page.getByRole('tab', { name: 'show-tasks/open_tasks' })
const chip2 = page.getByRole('tab', { name: 'excalidraw/create_view' })
const c1 = await chip1.count()
const c2 = await chip2.count()
if (c1 === 0 || c2 === 0) {
  // Tab strip may render chips as buttons rather than role=tab; retry by text.
  const t1 = await page.getByText('show-tasks/open_tasks', { exact: true }).count()
  const t2 = await page.getByText('excalidraw/create_view', { exact: true }).count()
  if (t1 === 0 || t2 === 0) {
    await page.screenshot({ path: join(OUT, 'FAILED-two-app-chips.png'), fullPage: false })
    throw new Error(`chips missing: role-tab counts=${c1}/${c2}, text counts=${t1}/${t2}`)
  }
}

await page.screenshot({ path: join(OUT, 'two-app-chips-dark.png'), fullPage: false })
console.log('captured', join(OUT, 'two-app-chips-dark.png'))

await browser.close()
srv.close()
