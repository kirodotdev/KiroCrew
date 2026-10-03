/**
 * Screenshot harness for the Warm Pool "Apply & Restart" button (#9191 / #9303).
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server,
 * with the boot-time /api calls answered by the shared `handleBootRoute` and
 * the /api/ws websocket intercepted by Playwright — no gateway, no dashboard
 * token. Only this harness's own scene fixtures live here.
 *
 * Scene: Developer → Config with a warmPool-capable provider. The Warm Pool
 * card renders its pool controls plus the RestartButton this change adds.
 * Clicking it POSTs /api/sessions/restart (fixture: ok) and the button
 * reports "config applied".
 *
 * Captures:
 *   warm-pool-card.png          Warm Pool card at rest, button visible
 *   warm-pool-restarted.png     success notice after clicking Apply & Restart
 *
 * Usage: node scripts/capture-warm-pool-restart.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { handleBootRoute, json, makeFixedApi } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/warm-pool-restart-button'
const PROJECT = '/home/kirocrew/workspace'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()

const fixedApi = makeFixedApi(PROJECT)
fixedApi.set('/api/status', {
  sessions: 3, messages: 128, cron_jobs: 0, subagents: 0, lessons: 4,
  uptime: 3600, version: '0.6.0',
})
fixedApi.set('/api/dashboard/branding', { bot_name: 'Kiro Crew', avatar: '/logo.png' })

const KIROCREW_CONFIG = {
  agents: { kirocrew: { provider: 'kiroacp', model: 'auto', approval_mode: 'reads', workspace: 'default', memory_store: 'default' } },
  default_agent: 'kirocrew',
  workspaces: { default: { dir: '~/.kiro/crew/workspace' } },
  default_workspace: 'default',
  memory_stores: { default: { description: 'Workspace memory', embedding_provider: 'local' } },
  default_memory_store: 'default',
  agent: { default_agent: 'kirocrew', provider: 'kiroacp', model: 'auto', approval_mode: 'reads', sandbox: 'auto', subagent_max_turns: 60, max_subagents: 8, subagent_auto_max: 4, conductor_skill: false, tool_search: true, max_channels: 5, max_channel_agents: 2, enforce_denied_commands: 'on' },
  session: { timeout_secs: 900, pool_size: 2, pool_agent: 'kirocrew', pool_ttl_secs: 900 },
  memory: { embedding_provider: 'local' },
  auto_update: true,
}

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1520, height: 1000 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()

let wsServer = null
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

const delegated = new Set()
await page.route('**/api/**', async route => {
  const req = route.request()
  const path = new URL(req.url()).pathname
  if (path === '/api/config/kirocrew' && req.method() === 'PATCH') return json(route, KIROCREW_CONFIG)
  if (path === '/api/config/kirocrew') return json(route, KIROCREW_CONFIG)
  if (path === '/api/sessions/restart' && req.method() === 'POST') {
    return json(route, { ok: true, sessions_reset: 2, mcp_synced: 3, mcp_sync_ok: true })
  }
  if (path === '/api/memory/settings') return json(route, { history_idle_hours: 3, history_max_days: 90, migrated: false })
  if (path === '/api/memory/preferences') return json(route, { content: '# User Preferences\n- Prefers dark mode' })
  if (path === '/api/memory/projects') return json(route, { content: '# Active Projects' })
  if (path === '/api/memory/history') return json(route, { content: '# 2026-07-27' })
  if (path === '/api/lessons') return json(route, { lessons: [
    { rule: 'Always run tsc -b before pushing frontend changes', category: 'tool', ts: new Date().toISOString() },
  ] })
  if (path === '/api/memory/stats') return json(route, { entries: 0, size_bytes: 0, provider: 'local' })
  if (path === '/api/memory/embedding-status') return json(route, { state: 'ready', model: 'all-MiniLM-L6-v2', downloaded: true })
  if (path === '/api/memory/semantic') return json(route, { entries: [] })
  if (path === '/api/memory/graph') return json(route, { nodes: [], edges: [] })
  if (path === '/api/agent-config' || path === '/api/agent/config') return json(route, {
    name: 'kirocrew', provider: 'kiroacp', tools: [], mcpServers: {},
  })
  if (path.includes('usage')) return json(route, {
    sessions: {
      total_sessions: 42,
      today: { sessions: 3, messages: 128, tool_calls: 61 },
      this_week: { sessions: 18, messages: 900, tool_calls: 400 },
      this_month: { sessions: 42, messages: 2100, tool_calls: 950 },
      avg_msgs_per_session: 50,
      daily_history: [
        { date: '2026-07-23', sessions: 5, messages: 380, tool_calls: 170 },
        { date: '2026-07-24', sessions: 6, messages: 545, tool_calls: 260 },
      ],
    },
    billing: { plan: 'Kiro Pro', credits_used: 633, credits_plan: 1000, resets: 'in 6h' },
  })
  delegated.add(path)
  return handleBootRoute(route, path, { project: PROJECT, theme: 'light', fixedApi })
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))

await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
})

const pushStatus = () => wsServer && wsServer.send(JSON.stringify({
  type: 'status',
  data: { sessions: 3, messages: 128, cron_jobs: 0, subagents: 0, lessons: 4, uptime: 3600, version: '0.6.0' },
}))
async function settle(ms = 1600) { await page.waitForTimeout(ms); pushStatus(); await page.waitForTimeout(600) }

// ---- Developer → Config, Warm Pool card
await page.goto(`${base}/developer?tab=config`, { waitUntil: 'domcontentloaded' })
await settle(2400)
const restartBtn = page.getByRole('button', { name: /Apply.*Restart/ })
await restartBtn.scrollIntoViewIfNeeded()
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/warm-pool-card.png` })

// ---- click Apply & Restart, capture the success notice
await restartBtn.click()
await page.getByText(/config applied/i).waitFor({ timeout: 10000 })
await page.waitForTimeout(400)
await page.screenshot({ path: `${OUT}/warm-pool-restarted.png` })

console.log('delegated to boot fixtures:', [...delegated].join(', ') || 'none')
await context.close()
await browser.close()
srv.close()
console.log('done')
