/**
 * Screenshot harness for the Apps page (hybrid Discover + Library).
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call and the /api/ws websocket intercepted by
 * Playwright and answered from fixtures — no gateway, no dashboard token.
 * Same technique as capture-overview.mjs.
 *
 * A subset of the fixture apps ship synthetic hero art so the captures show
 * both the 16:9 hero capsule and the gradient+icon fallback.
 *
 * Captures:
 *   discover.png           spotlight + feature duo + category rail + rows
 *   discover-category.png  category-filtered view (editorial layer collapses)
 *   sources.png            Sources popover (registries + install from path)
 *   library.png            Library tab with pending-updates banner
 *
 * Usage: node scripts/capture-apps.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/apps-shots'
const PORT = 6811
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' }
/** True only for a real regular file — a DIRECTORY must fall through to the
 *  SPA shell. dist/ contains a real `apps/` directory (built app-page assets)
 *  that shadows the /apps SPA route, so an existsSync-only check serves the
 *  directory, readFileSync throws EISDIR, and the page renders blank. */
const isFile = (p) => { try { return statSync(p).isFile() } catch { return false } }
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  // /logo.png is served by the gateway at runtime (channel-aware branding), so
  // it is absent from dist — map it to a packaged icon or the sidebar renders a
  // broken image in the captures.
  if (path === '/logo.png') {
    res.writeHead(200, { 'content-type': 'image/png' })
    res.end(readFileSync(join(DIST, 'icon-192.png')))
    return
  }
  // Containment: resolve inside DIST and reject anything that escapes it
  // (also covers encoded ../ traversal). Loopback-only, but keep it correct.
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) {
    res.writeHead(403); res.end(); return
  }
  if (path === '/' || !isFile(file)) file = join(DIST, 'index.html') // SPA fallback
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch {
    res.writeHead(404); res.end()
  }
})
await new Promise(r => server.listen(PORT, '127.0.0.1', r))

const status = { sessions: 12, messages: 4821, cron_jobs: 7, subagents: 3, lessons: 52, uptime: 273840, version: '0.1.0' }
const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

const REG = 'kirodotdev-labs'
const A = (name, displayName, author, description, tags, extra = {}) => ({
  name, displayName, author, description, tags, version: '1.0.0',
  installed: false, updateAvailable: false, ...extra,
})
const registryApps = [
  A('code-review-sage', 'Code Review Sage', 'kirocrew', 'Self-evolving deep code reviewer for GitHub PRs with a prioritized focus report.', ['code-review', 'github'], { featured: 1, version: '3.2.0' }),
  A('oncall-radar', 'Oncall Radar', 'kirocrew', 'Oncall operations dashboard — track tickets, pipelines, risks, MCMs, shift handoffs and maintain runbooks.', ['oncall', 'tickets'], { featured: 2, version: '2.1.0' }),
  A('auto-research', 'Research Lab', 'kirocrew', 'Autonomous research campaigns powered by autonudge — pre-flight validation, live findings, stagnation detection.', ['research', 'autonudge'], { featured: 3, installed: true, enabled: true, version: '1.4.0' }),
  A('issue-radar', 'Issue Radar', 'kirocrew', 'An issue triage assistant that remembers. Browse, filter, and triage GitHub issues with AI-suggested labels.', ['github', 'issue-triage'], { _registry: REG }),
  A('secretary', 'Secretary', 'zezhexu', 'Slack inbox manager — triage, draft replies, and digest channels.', ['slack', 'inbox'], { _registry: REG, installed: true, enabled: true, updateAvailable: true, installedVersion: '1.0.0', version: '1.1.0', lifecycle: 'gateway', origin: 'registry' }),
  A('taskkeeper', 'TaskKeeper', 'zezhexu', 'Personal task manager — triage Slack and email into actionable tasks with To-Do sync.', ['tasks', 'outlook'], { _registry: REG }),
  A('mimir', 'Mimir', 'zezhexu', 'Unified task aggregation across Taskei, SIM, and Asana.', ['tasks', 'aggregation'], { _registry: REG }),
  A('team-manager', 'Team Manager', 'zezhexu', 'Generate periodic team work reports and compare products with source-cited evidence.', ['reports', 'team'], { _registry: REG }),
  A('workflows', 'Workflows', 'kirocrew', 'Author, run, and watch dynamic workflows — agent-authored Python scripts that orchestrate KiroCrew agents.', ['workflows', 'automation'], { installed: true, enabled: true }),
  A('code-reviewer', 'Code Reviewer', 'kirocrew', 'Review local git changes with a CRUX-like diff viewer, inline comments, and an IntelliJ-style Git panel.', ['code-review', 'git'], { _registry: REG }),
  A('writing-review', 'WritingReview', 'zezhexu', 'Multi-scanner writing review for documents. Upload a doc, supply audience and tone context, and get findings grouped by scanner.', ['writing', 'review'], { _registry: REG }),
  A('auto-improvement', 'Auto-Improvement', 'zezhexu', 'Analyzes a target codebase, designs a calibrated metric, then runs keep-or-revert performance loops.', ['performance', 'code-quality'], { _registry: REG }),
]

const I = (name, displayName, origin, over = {}) => {
  const src = registryApps.find(a => a.name === name) || {}
  return {
    name, displayName, version: '1.0.0', enabled: true,
    installedAt: '2026-07-20T10:00:00Z', origin, resources: 'gateway',
    lifecycle: origin === 'builtin' ? 'locked' : 'gateway',
    manifest: { name, version: '1.0.0', displayName, description: src.description || '', author: src.author || 'kirocrew', tags: src.tags || [] },
    ...over,
  }
}
const installedApps = [
  I('secretary', 'Secretary', 'registry'),
  I('auto-research', 'Research Lab', 'builtin', { manifest: { ...I('auto-research', 'Research Lab', 'builtin').manifest, ui: { pages: [{ route: '/research', label: 'Research', icon: 'Search' }] } } }),
  I('workflows', 'Workflows', 'builtin', { manifest: { ...I('workflows', 'Workflows', 'builtin').manifest, ui: { pages: [{ route: '/workflows', label: 'Workflows', icon: 'Zap' }] } } }),
]

// Apps that ship hero art (the rest exercise the gradient fallback).
const HERO = {
  'code-review-sage': ['#2e1f57', '#6d4aff', 'Code Review Sage'],
  'oncall-radar': ['#4a1420', '#f0564f', 'Oncall Radar'],
  'auto-research': ['#0c3742', '#22d3ee', 'Research Lab'],
  'issue-radar': ['#4a3410', '#f59e0b', 'Issue Radar'],
  'secretary': ['#451a35', '#ec4899', 'Secretary'],
}
for (const name of Object.keys(HERO)) {
  const app = registryApps.find(a => a.name === name)
  app.heroImage = `/api/apps/blob?repo=${name}&path=hero.svg`
  app.heroImageDark = `/api/apps/blob?repo=${name}&path=hero-dark.svg`
}

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1520, height: 1000 }, deviceScaleFactor: 2 })
const page = await context.newPage()

let wsServer = null
await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

const unmatched = new Set()
await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  if (path === '/api/apps/blob') {
    const art = HERO[url.searchParams.get('repo') || '']
    if (!art) return route.fulfill({ status: 404, body: '' })
    const [from, to, label] = art
    return route.fulfill({
      status: 200,
      contentType: 'image/svg+xml',
      body: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 675"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${from}"/><stop offset="1" stop-color="${to}"/></linearGradient></defs><rect width="1200" height="675" fill="url(#g)"/><circle cx="980" cy="150" r="270" fill="#fff" opacity=".09"/><text x="64" y="600" font-family="Helvetica,Arial" font-size="60" font-weight="700" fill="#fff" opacity=".92">${label}</text></svg>`,
    })
  }
  if (path === '/api/apps/registry') return json(route, { apps: registryApps, serverPlatform: { os: 'darwin', arch: 'arm64' } })
  if (path === '/api/apps/registries') return json(route, { registries: [{ name: REG, repo: 'https://github.com/kirodotdev-labs/app-registry', branch: 'main' }] })
  if (path === '/api/apps') return json(route, installedApps)
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/kiro-prerequisite') return json(route, {
    platform: 'gateway', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: true,
    repair_required: false, docs_url: '', setup_allowed: false,
    operation: { status: 'idle', message: '' },
  })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/status') return json(route, status)
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/chat/slots') return json(route, [])
  if (path === '/api/models') return json(route, { models: [], default: 'auto' })
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  const objectish = /(config|tips|voice|autonudge|branding|status|themes)/.test(path)
  unmatched.add(path); console.log('UNMATCHED:', path)
  if (objectish) return json(route, {})
  return json(route, [])
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 600)))
await page.addInitScript(() => {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-theme-mode', 'dark')
})

const pushStatus = () => wsServer && wsServer.send(JSON.stringify({ type: 'status', data: status }))
async function settle(ms = 1600) { await page.waitForTimeout(ms); pushStatus(); await page.waitForTimeout(600) }

// ---- Discover (landing)
await page.goto(`http://127.0.0.1:${PORT}/apps`, { waitUntil: 'domcontentloaded' })
await settle(2400)
await page.screenshot({ path: `${OUT}/discover.png` })

// ---- category-filtered view (editorial layer collapses)
await page.getByRole('button', { name: /Developer Tools/ }).first().click()
await settle(1000)
await page.screenshot({ path: `${OUT}/discover-category.png` })
await page.getByRole('button', { name: /All apps/ }).first().click()
await settle(800)

// ---- Sources popover
await page.getByRole('button', { name: 'Manage app sources' }).click()
await settle(1000)
await page.screenshot({ path: `${OUT}/sources.png` })
await page.keyboard.press('Escape')
await settle(600)

// ---- Library (pending updates banner)
await page.getByText('Library').first().click()
await settle(1400)
await page.screenshot({ path: `${OUT}/library.png` })

console.log('unmatched /api paths:', [...unmatched].join(', ') || 'none')
await context.close()
await browser.close()
server.close()
console.log('done')
