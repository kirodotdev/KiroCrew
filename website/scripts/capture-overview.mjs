/**
 * Screenshot + recording harness for the Overview mission-control rewrite.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server,
 * with the boot-time /api calls answered by the shared `handleBootRoute` and the
 * /api/ws websocket intercepted by Playwright — no gateway, no dashboard token.
 * Only this harness's own scene fixtures live here.
 *
 * Captures:
 *   overview-landing.png        hero + tiles + summary cards
 *   overview-memory-drill.png   Memory browser drill-in
 *   overview-usage-drill.png    Usage report drill-in
 *   developer-memory-graph.png  relocated graph explorer
 *   developer-config.png        relocated config viewers
 *   import-export.png           Import / Export tab with backup section
 *   drillin.webm                landing -> Memory -> back -> Usage recording
 *
 * Usage: node scripts/capture-overview.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { handleBootRoute, json, makeFixedApi } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '/tmp/shots'
const PROJECT = '/home/kirocrew/workspace'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()

const status = {
  sessions: 12, messages: 4821, cron_jobs: 7, subagents: 3, lessons: 52,
  uptime: 273840, version: '0.1.0',
}

const fixedApi = makeFixedApi(PROJECT)
fixedApi.set('/api/status', status)
// TWO WORDS, matching the backend default: the nav brand row accents the last
// word only, so a single-word name renders the mark without its "CREW" half.
fixedApi.set('/api/dashboard/branding', { bot_name: 'Kiro Crew', avatar: '/logo.png' })

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1520, height: 1000 },
  deviceScaleFactor: 2,
  recordVideo: { dir: OUT, size: { width: 1520, height: 1000 } },
})
const page = await context.newPage()

let wsServer = null
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

// Paths this harness has no scene fixture for, and so hands to the shared boot
// table. Reported at the end as a hint, NOT as a failure: the boot table
// answers most of them, and it always fulfils, so nothing here ever hangs.
const delegated = new Set()
await page.route('**/api/**', async route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/memory/settings') return json(route, { history_idle_hours: 3, history_max_days: 90, migrated: false })
  if (path === '/api/memory/preferences') return json(route, { content: '# User Preferences\n- Prefers dark mode\n- Uses tabs, not spaces\n- Reviews PRs before merging' })
  if (path === '/api/memory/projects') return json(route, { content: '# Active Projects\n\n## Dashboard revamp\n- Status: mission-control Overview shipped\n\n## Docker image\n- Published to ghcr.io' })
  if (path === '/api/memory/history') return json(route, { content: '# 2026-07-27\n\n#### 09:12\nShipped the settings regroup.\n\n#### 14:30\nStarted the Overview rewrite.' })
  if (path === '/api/lessons') return json(route, { lessons: [
    { rule: 'Always run tsc -b before pushing frontend changes', category: 'tool', ts: new Date().toISOString() },
    { rule: 'Screenshots go under .github/screenshots/<feature>/', category: 'preference', ts: new Date().toISOString() },
  ] })
  if (path === '/api/memory/stats') return json(route, { entries: 128, size_bytes: 482000, provider: 'local' })
  if (path === '/api/memory/embedding-status') return json(route, { state: 'ready', model: 'all-MiniLM-L6-v2', downloaded: true })
  if (path === '/api/memory/semantic') return json(route, { entries: [] })
  if (path === '/api/memory/graph') return json(route, {
    nodes: [
      { id: 'pref-1', label: 'Dark mode', type: 'preference' },
      { id: 'proj-1', label: 'Dashboard revamp', type: 'project' },
      { id: 'lesson-1', label: 'tsc -b before push', type: 'lesson' },
      { id: 'hist-1', label: '2026-07-27', type: 'history' },
    ],
    edges: [
      { source: 'proj-1', target: 'hist-1' },
      { source: 'lesson-1', target: 'proj-1' },
    ],
  })
  if (path === '/api/kirocrew-config' || path === '/api/config/kirocrew') return json(route, {
    // Complete KiroCrewCfg shape (KiroCrewCfgTab enumerates every section).
    agents: { kirocrew: { provider: 'kiroacp', model: 'auto', approval_mode: 'reads' } },
    default_agent: 'kirocrew',
    workspaces: { default: { dir: '~/.kiro/crew/workspace' } },
    default_workspace: 'default',
    memory_stores: { default: { description: 'Workspace memory', embedding_provider: 'local' } },
    default_memory_store: 'default',
    agent: { default_agent: 'kirocrew', provider: 'kiroacp', model: 'auto', approval_mode: 'reads', sandbox: 'auto', subagent_max_turns: 60, max_subagents: 8, subagent_auto_max: 4, conductor_skill: false, tool_search: true, max_channels: 5, max_channel_agents: 2, enforce_denied_commands: 'on' },
    session: { timeout_secs: 900, pool_size: 2, pool_agent: 'kirocrew', pool_ttl_secs: 900 },
    memory: { embedding_provider: 'local' },
    auto_update: true,
  })
  if (path === '/api/agent-config' || path === '/api/agent/config') return json(route, {
    name: 'kirocrew', provider: 'kiroacp', tools: ['fs_read', 'fs_write', 'execute_bash'], mcpServers: { 'playwright-mcp': {} },
  })
  // Provider usage — kirocrew provider raw payload (normalized client-side).
  if (path.includes('usage')) return json(route, {
    // Raw AcpAdapter.fetchUsage() contract (snake_case; normalized client-side).
    sessions: {
      total_sessions: 42,
      today: { sessions: 3, messages: 128, tool_calls: 61 },
      this_week: { sessions: 18, messages: 900, tool_calls: 400 },
      this_month: { sessions: 42, messages: 2100, tool_calls: 950 },
      avg_msgs_per_session: 50,
      daily_history: [
        { date: '2026-07-23', sessions: 5, messages: 380, tool_calls: 170 },
        { date: '2026-07-24', sessions: 6, messages: 545, tool_calls: 260 },
        { date: '2026-07-25', sessions: 2, messages: 190, tool_calls: 75 },
        { date: '2026-07-26', sessions: 4, messages: 410, tool_calls: 190 },
        { date: '2026-07-27', sessions: 3, messages: 128, tool_calls: 61 },
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

const pushStatus = () => wsServer && wsServer.send(JSON.stringify({ type: 'status', data: status }))

async function settle(ms = 1600) { await page.waitForTimeout(ms); pushStatus(); await page.waitForTimeout(600) }

// ---- landing
await page.goto(`${base}/settings?tab=overview`, { waitUntil: 'domcontentloaded' })
await settle(2400)
await page.screenshot({ path: `${OUT}/overview-landing.png` })

// ---- drill-in recording: Memory -> back -> Usage -> back
const details = page.getByRole('button', { name: 'View details' })
await details.nth(1).click(); await settle(1200)
await page.screenshot({ path: `${OUT}/overview-memory-drill.png` })
await page.getByRole('button', { name: 'Back to Overview' }).click(); await settle(800)
await details.nth(0).click(); await settle(1200)
await page.screenshot({ path: `${OUT}/overview-usage-drill.png` })
await page.getByRole('button', { name: 'Back to Overview' }).click(); await settle(800)

// ---- developer page: relocated graph + configs
await page.goto(`${base}/developer?tab=memory`, { waitUntil: 'domcontentloaded' })
await settle(2000)
await page.screenshot({ path: `${OUT}/developer-memory-graph.png` })
await page.goto(`${base}/developer?tab=config`, { waitUntil: 'domcontentloaded' })
await settle(1600)
await page.screenshot({ path: `${OUT}/developer-config.png` })

// ---- import / export tab
await page.goto(`${base}/settings?tab=imports`, { waitUntil: 'domcontentloaded' })
await settle(1400)
await page.screenshot({ path: `${OUT}/import-export.png` })

console.log('delegated to boot fixtures:', [...delegated].join(', ') || 'none')
await context.close() // flushes the video
await browser.close()
srv.close()
console.log('done')
