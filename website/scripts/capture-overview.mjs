/**
 * Screenshot + recording harness for the Overview mission-control rewrite.
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call and the /api/ws websocket intercepted by
 * Playwright and answered from fixtures — no gateway, no dashboard token.
 * Same technique as capture-pr-state-sync.mjs.
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
import { mkdirSync, readFileSync, existsSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/shots'
const PORT = 6807
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' }
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  // Containment: resolve inside DIST and reject anything that escapes it
  // (also covers encoded ../ traversal). Loopback-only, but keep it correct.
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) {
    res.writeHead(403); res.end(); return
  }
  if (!existsSync(file) || path === '/') file = join(DIST, 'index.html') // SPA fallback
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch {
    res.writeHead(404); res.end()
  }
})
await new Promise(r => server.listen(PORT, '127.0.0.1', r))

const status = {
  sessions: 12, messages: 4821, cron_jobs: 7, subagents: 3, lessons: 52,
  uptime: 273840, version: '0.1.0',
}

const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1520, height: 1000 },
  deviceScaleFactor: 2,
  recordVideo: { dir: OUT, size: { width: 1520, height: 1000 } },
})
const page = await context.newPage()

let wsServer = null
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

const unmatched = new Set()
await page.route('**/api/**', async route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/kiro-prerequisite') return json(route, {
    platform: 'gateway', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: true,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { status: 'idle', message: '' },
  })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/status') return json(route, status)
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
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '' })
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  if (path === '/api/models') return json(route, { models: [], default: 'auto' })
  if (path === '/api/chat/slots') return json(route, [])
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
  const objectish = /(config|tips|voice|autonudge|branding|status|themes)/.test(path)
  unmatched.add(path); console.log('UNMATCHED:', path)
  if (objectish) return json(route, {})
  return json(route, [])
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))

await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
})

const pushStatus = () => wsServer && wsServer.send(JSON.stringify({ type: 'status', data: status }))

async function settle(ms = 1600) { await page.waitForTimeout(ms); pushStatus(); await page.waitForTimeout(600) }

// ---- landing
await page.goto(`http://127.0.0.1:${PORT}/settings?tab=overview`, { waitUntil: 'domcontentloaded' })
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
await page.goto(`http://127.0.0.1:${PORT}/developer?tab=memory`, { waitUntil: 'domcontentloaded' })
await settle(2000)
await page.screenshot({ path: `${OUT}/developer-memory-graph.png` })
await page.goto(`http://127.0.0.1:${PORT}/developer?tab=config`, { waitUntil: 'domcontentloaded' })
await settle(1600)
await page.screenshot({ path: `${OUT}/developer-config.png` })

// ---- import / export tab
await page.goto(`http://127.0.0.1:${PORT}/settings?tab=imports`, { waitUntil: 'domcontentloaded' })
await settle(1400)
await page.screenshot({ path: `${OUT}/import-export.png` })

console.log('unmatched /api paths:', [...unmatched].join(', ') || 'none')
await context.close() // flushes the video
await browser.close()
server.close()
console.log('done')
