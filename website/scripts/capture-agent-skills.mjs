/**
 * Screenshot harness for mapping skills to an agent template.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback, and answers every /api/** call from fixtures via Playwright
 * route interception. No gateway, no dashboard auth, no kiro-cli spawn.
 *
 * The client code under test is unmodified — only the network is stubbed — so
 * Agent Capabilities > Agent Templates, the Skills chips, the add-skill
 * dropdown, and the PATCH round-trip are exercised exactly as they run in
 * production. The stubbed PATCH mutates the fixture, so the after-shot shows
 * the real re-render, not a mock.
 *
 * Usage: node scripts/capture-agent-skills.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const OUT = process.argv[2] || '/tmp/agent-skills-shots'
// fileURLToPath, not URL.pathname: on Windows .pathname yields "/C:/…", which
// join() then turns into an invalid "\C:\…" and every read fails with ENOENT.
const DIST = fileURLToPath(new URL('../dist/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ico': 'image/x-icon',
}

/** Static server with index.html fallback so /capabilities deep-links resolve. */
function serveDist() {
  return new Promise(resolve => {
    const srv = createServer((req, res) => {
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '')
      let file = join(DIST, rel)
      if (!rel || !existsSync(file) || statSync(file).isDirectory()) file = join(DIST, 'index.html')
      res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' })
      res.end(readFileSync(file))
    })
    srv.listen(0, '127.0.0.1', () => resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }))
  })
}

const SKILLS = [
  { key: 'babysit', name: 'babysit', description: 'Same-session monitoring loop for PRs and CI runs', path: '/home/user/.kiro/crew/skills/babysit/SKILL.md', source: 'kirocrew' },
  { key: 'prepare-pr', name: 'prepare-pr', description: 'Drive working-tree changes to a review-ready pull request', path: '/home/user/.kiro/crew/skills/prepare-pr/SKILL.md', source: 'kirocrew' },
  { key: 'rubber-duck', name: 'rubber-duck', description: 'Adversarial review that turns explaining out loud into a hallucination check', path: '/home/user/.kiro/crew/skills/rubber-duck/SKILL.md', source: 'kirocrew' },
  { key: 'widgets', name: 'widgets', description: 'Render rich HTML inline via mcwidget tags', path: '/home/user/.kiro/crew/skills/widgets/SKILL.md', source: 'kirocrew' },
  { key: 'kiro-user/pod-e2e', name: 'pod-e2e', description: 'Run end-to-end tests against an isolated throwaway pod', path: '/home/user/.kiro/skills/pod-e2e/SKILL.md', source: 'kiro-user' },
  { key: 'kiro-user/llm-council', name: 'llm-council', description: 'Convene a cross-vendor LLM council for hard decisions', path: '/home/user/.kiro/skills/llm-council/SKILL.md', source: 'kiro-user' },
]

/** Mutated by the stubbed PATCH so the after-shot renders real state. */
const mapping = {
  kirocrew: [],
  'code-reviewer': ['prepare-pr', 'rubber-duck'],
  'release-captain': [],
}
const UNMANAGED = { 'release-captain': ['skill://~/.kiro/skills/*/SKILL.md'] }

const AGENTS = [
  { name: 'kirocrew', description: 'Autonomous personal AI agent', source: 'kirocrew', model: 'claude-opus-4.8', mcp_servers: ['kirocrew-core', 'kirocrew-cron'], filename: 'kirocrew.json' },
  { name: 'code-reviewer', description: 'Reviews code changes against the repo conventions', source: 'builtin', model: 'claude-sonnet-4.5', mcp_servers: [], filename: 'code-reviewer.json' },
  { name: 'release-captain', description: 'Cuts releases and babysits the pipeline', source: 'builtin', model: 'auto', mcp_servers: [], filename: 'release-captain.json' },
]

const DETAIL = {
  kirocrew: {
    name: 'kirocrew',
    description: 'Autonomous personal AI agent',
    model: 'claude-opus-4.8',
    tools: ['execute_bash', 'fs_read', 'fs_write', 'code', 'grep', 'glob'],
    mcpServers: { 'kirocrew-core': {}, 'kirocrew-cron': {} },
  },
  'code-reviewer': {
    name: 'code-reviewer',
    description: 'Reviews code changes against the repo conventions',
    model: 'claude-sonnet-4.5',
    tools: ['fs_read', 'grep', 'glob'],
    allowedTools: ['fs_read', 'grep'],
  },
  'release-captain': {
    name: 'release-captain',
    description: 'Cuts releases and babysits the pipeline',
    model: 'auto',
    tools: ['fs_read', 'execute_bash'],
  },
}

function installed() {
  return AGENTS.map(a => ({
    ...a,
    skills: [
      ...(mapping[a.name] || []).map(k => k.split('/').pop()),
      ...(UNMANAGED[a.name] || []).map(() => '*'),
    ],
  }))
}

const json = (route, body) =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  // Chips and labels are 11–13px type; a 1x shot renders soft on GitHub.
  deviceScaleFactor: 2,
})
const page = await context.newPage()

await page.routeWebSocket(/\/api\/ws/, () => {})

await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  const method = route.request().method()

  if (path.startsWith('/api/agents/detail/')) {
    const name = decodeURIComponent(path.split('/').pop())
    if (method === 'PATCH') {
      const body = JSON.parse(route.request().postData() || '{}')
      if (Array.isArray(body.skills)) mapping[name] = body.skills
      return json(route, { ok: true, model: DETAIL[name]?.model || '', skills: mapping[name] })
    }
    return json(route, {
      ...(DETAIL[name] || { name }),
      skills: mapping[name] || [],
      unmanaged_skills: UNMANAGED[name] || [],
    })
  }
  if (path === '/api/agents/installed') return json(route, installed())
  if (path === '/api/skills') return json(route, SKILLS)
  if (path === '/api/config/default-agent') return json(route, { default_agent: 'kirocrew' })
  if (path.startsWith('/api/agent-metadata/')) return json(route, { content: '' })
  if (path === '/api/mcp/probe') return json(route, [])
  if (path === '/api/spawn') return json(route, { agents: [] })
  if (path === '/api/sessions/context') return json(route, { sessions: [] })
  if (path === '/api/sessions/usage') return json(route, { usage: null })
  if (path === '/api/models') {
    return json(route, [
      { model_name: 'auto', description: 'Let Kiro choose' },
      { model_name: 'claude-opus-4.8', description: 'Most capable' },
      { model_name: 'claude-sonnet-4.5', description: 'Balanced' },
    ])
  }
  // The app shell mounts behind this gate and reads status.operation.status —
  // a generic object stub crashes it, blanking the whole page.
  if (path === '/api/kiro-prerequisite') {
    return json(route, {
      platform: 'linux', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: false,
      repair_required: false, docs_url: '', setup_allowed: false,
      operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
    })
  }
  if (path === '/api/chat/slots') return json(route, [])
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, subagents: 0, uptime: 120, version: 'dev' })
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
  if (path === '/api/recent-projects') return json(route, { dirs: [] })
  if (path === '/api/dashboard/config') return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
  const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
  return json(route, objectish ? {} : [])
})

page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

async function load() {
  await page.goto(base + '/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2400)
}

const shot = async name => {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

/** Crop to the Installed Agents card — the list + detail panel are the story. */
async function card(name, pad = 16) {
  const heading = page.getByRole('heading', { name: /Installed Agents/ })
  if (await heading.count()) {
    const hb = await heading.first().boundingBox()
    if (hb) {
      const y0 = Math.max(0, hb.y - pad * 2)
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: { x: Math.max(0, hb.x - pad * 2), y: y0, width: 1440, height: Math.min(950 - y0, 560) },
      })
      console.log('wrote', `${OUT}/${name}.png`)
      return
    }
  }
  await shot(name)
}

async function selectAgent(name) {
  await page.getByText(name, { exact: true }).first().click()
  await page.waitForTimeout(900)
}

await load()

// 1. An agent with no mapping: the honest empty state, not a silent blank.
await selectAgent('kirocrew')
await shot('01-agent-templates-no-mapping')
await card('02-no-mapping-card')

// 2. An agent with skills already mapped — chips carry catalog display names.
await selectAgent('code-reviewer')
await card('03-mapped-skills-chips')

// 3. The add-skill dropdown, filtered to the catalog minus what's mapped.
await page.getByRole('button', { name: /add skill/i }).click()
await page.waitForTimeout(500)
await shot('04-add-skill-dropdown')

// 4. After adding: the chip row re-renders from the PATCH response.
await page.getByRole('option', { name: /babysit/i }).click()
await page.waitForTimeout(900)
await card('05-after-add')

// 5. A hand-authored wildcard mapping: read-only, no remove control.
await selectAgent('release-captain')
await card('06-unmanaged-wildcard')

await browser.close()
srv.close()
console.log('done')
