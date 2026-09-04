/**
 * Screenshot harness for the active repository's FORGE surviving the host page.
 *
 * Runs the REAL built SPA (website/dist) gateway-free -- every /api/** answered
 * from fixtures, no gateway, no `glab` CLI, no GitLab instance -- the same
 * technique as capture-issue-radar-azure.mjs. Fixtures are what make these frames
 * capturable at all: a self-managed GitLab instance is not something the build
 * host holds a credential for, so a live capture is impossible here rather than
 * merely inconvenient.
 *
 * WHAT THE FRAMES ARE EVIDENCE OF. `IssueRadarPage` used to resolve the active
 * repository two ways and both dropped the forge: the fallback arm built
 * `{owner, repo}` out of `repos[0]`, and the membership test compared the slug
 * alone so a stored slug-only pointer came back unenriched. A forge-less ref reads
 * as public GitHub (`repoScopeKey` resolves the absent fields that way), so the
 * whole app rendered GitHub's marks, host tag and change-request vocabulary over a
 * GitLab project. Every scene drives the localStorage pointer the page actually
 * reads, so what is photographed is the resolution, not a prop set by hand.
 *
 * Captures:
 *   gl-01-fallback-forge.png    nothing stored: the fallback arm must carry
 *                               repos[0]'s provider and host
 *   gl-02-legacy-pointer.png    a stored slug-only pointer: healed from the
 *                               connected record rather than handed back bare
 *   gl-03-mixed-forge.png       one slug on two forges, switcher open: the rows
 *                               are distinguishable and the active one is right
 *
 * Usage: node scripts/capture-issue-radar-active-forge.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/gl-forge-shots'
const PORT = 6843
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' }
const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) { res.writeHead(403); res.end(); return }
  if (!existsSync(file) || path === '/') file = join(DIST, 'index.html')
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch { res.writeHead(404); res.end() }
})
await new Promise(r => server.listen(PORT, '127.0.0.1', r))

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

// One slug on two forges -- the collision a slug-only match cannot express. The
// GitLab project is a nested group path, which is the shape `owner` carries there.
const GL = { owner: 'acme/infra', repo: 'widget', provider: 'gitlab', host: 'gitlab.acme.internal' }
const GH = { owner: 'acme/infra', repo: 'widget', provider: 'github', host: 'github.com' }

// The GitLab record grants writes and the GitHub one does not, so `canWrite` is
// part of the frame: reading the wrong record shows the wrong affordances.
const GL_REC = { ...GL, enabled: true, permissions: { push: true, triage: true } }
const GH_REC = { ...GH, enabled: true, permissions: { push: false, triage: false } }

const GL_ISSUES = [
  { number: 412, title: 'Runner cache is not shared between the build and test jobs', url: '#', labels: ['bug'], comments: 3, updated_at: '2026-08-28T00:00:00Z', created_at: '2026-08-22T00:00:00Z', state: 'open', author: 'ada', assignees: ['ada'] },
  { number: 418, title: 'Publish the module registry index on tag', url: '#', labels: ['enhancement'], comments: 1, updated_at: '2026-08-27T00:00:00Z', created_at: '2026-08-25T00:00:00Z', state: 'open', author: 'grace', assignees: [] },
  { number: 421, title: 'Terraform plan output is truncated in the job log', url: '#', labels: ['bug'], comments: 0, updated_at: '2026-08-26T00:00:00Z', created_at: '2026-08-26T00:00:00Z', state: 'open', author: 'grace', assignees: [] },
]
const GH_ISSUES = [
  { number: 7, title: 'A same-slug repository that must not be confused for the GitLab one', url: '#', labels: ['bug'], comments: 2, updated_at: '2026-08-28T00:00:00Z', created_at: '2026-08-20T00:00:00Z', state: 'open', author: 'alice', assignees: [] },
]

const LABELS = [
  { name: 'bug', color: 'd73a4a', description: "Something isn't working" },
  { name: 'enhancement', color: 'a2eeef', description: 'New feature or request' },
]

/** Which fixture repo a request is about, read from its own query string.
 *
 * This is the whole point of the change under test: a request that names no
 * provider is indistinguishable from one that says `github`, so before the fix a
 * GitLab project's reads arrived here looking like GitHub's. */
const refOf = q => (q.get('provider') === 'gitlab' ? GL : GH)

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()
await page.routeWebSocket(/\/api\/ws/, () => {})

const unmatched = new Set()
let repos = { repos: [GL_REC] }

await page.route('**/api/**', async route => {
  const url = new URL(route.request().url())
  const path = url.pathname
  const q = url.searchParams
  const ref = refOf(q)
  const isGl = ref === GL
  // ── boot endpoints ──
  if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
  if (path === '/api/kiro-prerequisite') return json(route, { platform: 'gateway', installed: true, authenticated: true, ready: true, initial_setup_complete: true, can_auto_install: false, can_login: true, repair_required: false, docs_url: '', setup_allowed: false, operation: { status: 'idle', message: '' } })
  if (path === '/api/themes') return json(route, { themes: [], installed: [] })
  if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '' })
  if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '' })
  if (path === '/api/dashboard/config') return json(route, {})
  if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
  if (path === '/api/status') return json(route, { sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, uptime: 1000, version: '0.1.0' })
  if (path === '/api/chat/slots') return json(route, [])
  if (path === '/api/chat/folders') return json(route, [])
  if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
  // ── issue-radar ──
  if (path.endsWith('/issue-radar/repos')) return json(route, repos)
  if (path.endsWith('/issue-radar/me')) return json(route, { login: 'owner', provider: ref.provider, host: ref.host })
  if (path.endsWith('/issue-radar/labels')) return json(route, { ...ref, from_cache: true, labels: LABELS })
  if (path.endsWith('/issue-radar/members')) return json(route, { ...ref, members: [{ login: 'ada', role: 'admin' }], source: 'collaborators', from_cache: true })
  if (path.endsWith('/issue-radar/settings')) return json(route, { ...ref, settings: { triage_labels: [], unlabeled_is_untriaged: true, good_first_issue_labels: [], notify_on_new_issue: false, revision: 1 } })
  if (path.endsWith('/issue-radar/issues')) return json(route, { ...ref, state: 'open', from_cache: true, issues: isGl ? GL_ISSUES : GH_ISSUES })
  if (path.endsWith('/issue-radar/pulls')) return json(route, { ...ref, state: 'open', from_cache: true, bulk_max: 50, pulls: [] })
  if (path.endsWith('/issue-radar/pulls/search')) return json(route, { ...ref, state: 'open', from_cache: true, pulls: [] })
  if (path.endsWith('/issue-radar/pull-ai') || path.endsWith('/issue-radar/issue-ai')) return json(route, { ...ref, number: 412, summary: '', suggested_labels: [], from_cache: true })
  if (path.endsWith('/issue-radar/recent-repos')) return json(route, { repos: [] })
  // Boot endpoints that return LISTS (the dashboard .maps/.filters them). Default
  // object `{}` for everything else. Guessing array-vs-object wrong crashes the
  // SPA behind its error boundary (`_.filter is not a function`).
  unmatched.add(path)
  if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/approvals'
    || path === '/api/terminal/sessions' || path.endsWith('/pending')) return json(route, [])
  const listish = /(agents|sessions|projects|slots|folders|list|apps)$/.test(path)
  return json(route, listish ? [] : {})
})

page.on('pageerror', err => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
await page.addInitScript(() => { localStorage.setItem('mc-onboarded', '1') })
const settle = (ms = 2400) => page.waitForTimeout(ms)

/** Seed the pointer the page reads, then open the app. `active: null` removes it,
 *  which is the first-visit state the fallback arm serves. */
async function open(active, ui) {
  await page.addInitScript((s) => {
    if (s.active) localStorage.setItem('kc:issue-radar:active-repo', JSON.stringify(s.active))
    else localStorage.removeItem('kc:issue-radar:active-repo')
    if (s.ui) localStorage.setItem('kc:issue-radar:ui-state', JSON.stringify(s.ui))
    else localStorage.removeItem('kc:issue-radar:ui-state')
  }, { active, ui })
  await page.goto(`http://127.0.0.1:${PORT}/issue-radar`, { waitUntil: 'domcontentloaded' })
  await settle()
}

const ISSUES_UI = { mainView: 'issues', stateFilter: 'open' }

/** Fail loudly rather than emit a frame of the wrong state. */
async function mustSee(...texts) {
  for (const t of texts) await page.getByText(t, { exact: false }).first().waitFor({ timeout: 15000 })
}

// ── 01: first visit, nothing stored. The fallback arm has to carry repos[0]'s
// provider and host; before the fix it built `{owner, repo}` and this whole app
// rendered GitHub's marks and vocabulary over a GitLab project.
await open(null, ISSUES_UI)
await mustSee('gitlab.acme.internal', 'Runner cache is not shared')
await page.screenshot({ path: `${OUT}/gl-01-fallback-forge.png` })

// ── 02: a stored pointer with no forge on it -- the legacy-upgrade shape
// `loadActiveRepo` accepts on purpose. It must be HEALED from the connected
// record, not matched on the slug and handed back bare.
repos = { repos: [GL_REC] }
await open({ owner: GL.owner, repo: GL.repo }, ISSUES_UI)
await mustSee('gitlab.acme.internal')
await page.screenshot({ path: `${OUT}/gl-02-legacy-pointer.png` })

// ── 03: the same slug on two forges, switcher open. Two rows whose `owner/repo`
// is identical, so the marks and host tags are the only thing telling them apart
// -- and the active one has to be the one the pointer actually names.
repos = { repos: [GL_REC, GH_REC] }
await open({ ...GL }, ISSUES_UI)
await page.getByTestId('repo-path-label').click()
await page.waitForTimeout(700)
await page.screenshot({ path: `${OUT}/gl-03-mixed-forge.png` })

console.log('unmatched /api paths:', [...unmatched].join(', ') || 'none')
await context.close(); await browser.close(); server.close()
console.log('done ->', OUT)
