/**
 * Screenshot harness for #28285: Issue Radar offers only the review verdicts the
 * provider records.
 *
 * Runs the REAL built SPA (website/dist) on a static server bound to 127.0.0.1,
 * with every /api/** call and the /api/ws websocket answered from fixtures by
 * Playwright: no gateway, no `gh` / `glab` / `az`, no provider calls. Same
 * technique and fixture shapes as capture-issue-radar-pr-actions.mjs.
 *
 * Captures, one open pull / merge request per provider with its head known:
 *   verdicts-github.png      per-PR bar: Approve and Request changes (the control)
 *   verdicts-gitlab.png      per-MR bar: Approve only (GitLab has no request-changes review)
 *   verdicts-azure.png       per-PR bar: no verdict buttons (Azure DevOps records neither)
 *   bulk-azure.png           bulk bar over two rows: no Approve
 *
 * Usage: node scripts/capture-28285-review-verdicts.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/review-verdicts'
const PORT = 6829
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2',
  '.json': 'application/json',
}
const isFile = (p) => { try { return statSync(p).isFile() } catch { return false } }

const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  if (path === '/logo.png') {
    res.writeHead(200, { 'content-type': 'image/png' })
    res.end(readFileSync(join(DIST, 'icon-192.png')))
    return
  }
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) {
    res.writeHead(403); res.end(); return
  }
  if (path === '/' || !isFile(file)) file = join(DIST, 'index.html')
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch {
    res.writeHead(404); res.end()
  }
})

const ISO = '2026-11-02T18:00:00Z'

const PROVIDERS = {
  github: {
    repo: { owner: 'kirodotdev', repo: 'KiroCrew', provider: 'github', host: 'github.com' },
    url: (n) => `https://github.com/kirodotdev/KiroCrew/pull/${n}`,
  },
  gitlab: {
    repo: { owner: 'acme', repo: 'widget', provider: 'gitlab', host: 'gitlab.com' },
    url: (n) => `https://gitlab.com/acme/widget/-/merge_requests/${n}`,
  },
  azure: {
    repo: { owner: 'contoso/platform', repo: 'service', provider: 'azure', host: 'dev.azure.com' },
    url: (n) => `https://dev.azure.com/contoso/platform/_git/service/pullrequest/${n}`,
  },
}

const scene = { provider: 'github' }

function pulls() {
  const { url } = PROVIDERS[scene.provider]
  const row = (number, title, headSha) => ({
    number, title, url: url(number), state: 'open', draft: false,
    labels: [], author: 'alice', author_association: 'MEMBER',
    created_at: ISO, updated_at: ISO, closed_at: null, merged_at: null,
    assignees: [], requested_reviewers: [],
    base: 'main', head: `feat/${number}`, head_sha: headSha,
    additions: 40, deletions: 3, changed_files: 4,
    checks_state: 'success', checks_counts: { failure: 0, running: 0, success: 6, other: 0 },
    body: 'Adds a regression test.',
  })
  return [row(1111, 'fix(api): keep the cursor on a refused page', 'a1b2c3d4e5f6'),
    row(1102, 'fix(nav): keep the rail width across a reload', '9f8e7d6c5b4a')]
}

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  await new Promise((r) => server.listen(PORT, '127.0.0.1', r))
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1680, height: 950 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const IR = '/api/apps/issue-radar'
    const REPO = PROVIDERS[scene.provider].repo
    if (path === `${IR}/repos`) {
      return json(route, {
        repos: [{
          ...REPO, slug: `${REPO.owner}/${REPO.repo}`,
          permissions: { triage: true, push: true, maintain: false, admin: false },
        }],
      })
    }
    if (path === `${IR}/me`) return json(route, { login: 'alice' })
    if (path === `${IR}/pulls` || path === `${IR}/pulls/search`) {
      return json(route, { ...REPO, pulls: pulls(), from_cache: false, bulk_max: 50 })
    }
    if (path === `${IR}/pull`) {
      return json(route, {
        ...REPO, number: 1111,
        detail: {
          ...pulls()[0], merged: false, merged_by: null, comments: 2, review_comments: 3,
          commits: 1, mergeable: true, mergeable_state: 'blocked', labels: [],
          milestone: null, auto_merge: null,
        },
        timeline: [], checks: [], from_cache: false,
      })
    }
    if (path === `${IR}/pull/runs`) return json(route, { ...REPO, number: 1111, runs: [] })
    if (path === `${IR}/labels`) return json(route, { ...REPO, labels: [] })
    if (path === `${IR}/members`) {
      return json(route, { ...REPO, from_cache: false, source: 'collaborators', members: [{ login: 'alice', role: 'write' }] })
    }
    if (path === `${IR}/settings`) {
      return json(route, {
        ...REPO,
        settings: {
          triage_labels: [], unlabeled_is_untriaged: true,
          good_first_issue_labels: [], notify_on_new_issue: false, revision: 1,
        },
      })
    }
    if (path === `${IR}/issues`) return json(route, { ...REPO, issues: [], from_cache: false })
    if (path === `${IR}/recent-repos`) return json(route, { repos: [] })
    if (path === `${IR}/tagging`) return json(route, { ...REPO, issues: [], suggestions: [], titles: [], bulk_max: 50 })
    if (path === `${IR}/recommendations`) return json(route, { ...REPO, recommendations: [] })
    if (path === `${IR}/investigation`) return json(route, { ...REPO, record: null })
    // The detail pane's "Blocked by / Blocking" section iterates `edges`.
    if (path === `${IR}/deps`) return json(route, { schema: 1, edges: [], nodes: {} })
    if (path.startsWith(`${IR}/`)) return json(route, {})

    // Dashboard shell, the same answers as capture-issue-radar-pr-actions.mjs.
    if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/apps') {
      return json(route, [{
        name: 'issue-radar', displayName: 'Issue Radar', enabled: true, builtin: true, version: '0.1.0',
        ui: { pages: [{ route: '/issue-radar', label: 'Issue Radar', icon: 'Radar' }] },
      }])
    }
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '', language: 'en' })
    if (path.startsWith('/api/effort-levels')) return json(route, ['low', 'medium', 'high', 'xhigh', 'max'])
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/chat/slots') return json(route, [])
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/agents') {
      return json(route, { agents: [{ name: 'default', kiro_agent: 'kirocrew', description: 'Default crew agent' }], default_agent: 'default' })
    }
    if (path.startsWith('/api/agents/detail/')) return json(route, { name: 'kirocrew', model: 'claude-opus-5', skills: [] })
    if (path === '/api/agents/installed') return json(route, [])
    if (path === '/api/approvals') return json(route, [])
    if (path === '/api/terminal/sessions') return json(route, { sessions: [] })
    if (path === '/api/sessions/usage') return json(route, { sessions: [] })
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'gateway', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: true,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { status: 'idle', message: '' },
      })
    }
    if (path === '/api/ask-question/pending') return json(route, { questions: [] })
    if (path === '/api/models') return json(route, { models: [], default: 'auto' })
    const objectish = /(config|tips|voice|autonudge|branding|status|themes|usage|sync)/.test(path)
    return json(route, objectish ? {} : [])
  })

  page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 900)))

  async function load(provider) {
    scene.provider = provider
    await page.addInitScript((repo) => {
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-theme-mode', 'dark')
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('kc:issue-radar:active-repo', JSON.stringify(repo))
      localStorage.setItem('kc:issue-radar:ui-state', JSON.stringify({
        mainView: 'pulls', prStateFilter: 'open', prSelectedPull: 1111,
      }))
    }, PROVIDERS[provider].repo)
    await page.goto(`http://127.0.0.1:${PORT}/issue-radar`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3000)
  }

  async function openPull(number) {
    const row = page.getByText(new RegExp(`[#!]${number}\\b`)).first()
    if (await row.count()) {
      await row.click().catch(() => {})
      await page.waitForTimeout(1800)
    }
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  for (const provider of ['github', 'gitlab', 'azure']) {
    await load(provider)
    await openPull(1111)
    const approve = await page.getByRole('button', { name: /^approve$/i }).count()
    const changes = await page.getByRole('button', { name: /request changes/i }).count()
    console.log(`${provider}: approve=${approve} request_changes=${changes}`)
    await shot(`verdicts-${provider}`)
  }

  await load('azure')
  const boxes = page.locator('input[type="checkbox"]')
  for (let i = 0; i < Math.min(await boxes.count(), 2); i++) {
    await boxes.nth(i).click().catch(() => {})
    await page.waitForTimeout(400)
  }
  await page.waitForTimeout(900)
  console.log(`azure bulk: approve=${await page.getByRole('button', { name: /^approve$/i }).count()}`)
  await shot('bulk-azure')

  await context.close()
  await browser.close()
  server.close()
  console.log('done')
}

main()
