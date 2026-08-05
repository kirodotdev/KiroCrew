/**
 * Screenshot harness for Issue Radar's pull-request ACTION surface.
 *
 * Runs the REAL built SPA (website/dist) on a tiny static server with SPA
 * fallback, with every /api/** call and the /api/ws websocket intercepted by
 * Playwright and answered from fixtures — no gateway, no `gh`, no provider
 * calls. Same technique as capture-apps.mjs / capture-pr-state-sync.mjs.
 *
 * The client code under test is unmodified: only the network is stubbed, so the
 * per-PR bar, the composer, the merge gate, the bulk bar and its typed
 * confirmation are exercised exactly as they run in production.
 *
 * Captures:
 *   01-pr-actions-bar        per-PR bar on a mergeable PR (all verbs offered)
 *   02-review-composer       the approve composer — a verdict needs prose, not a click
 *   03-merge-blocked         a BLOCKED PR: no Merge, auto-merge is the affordance
 *   04-bulk-bar              bulk bar over a two-PR selection
 *   05-bulk-close-confirm    the typed confirmation a bulk close requires
 *   06-pr-actions-bar-light  the per-PR bar in the light theme
 *
 * Usage: node scripts/capture-issue-radar-pr-actions.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join, resolve, sep } from 'node:path'

const OUT = process.argv[2] || '/tmp/issue-radar-pr-actions'
const PORT = 6813
const DIST = new URL('../dist', import.meta.url).pathname
mkdirSync(OUT, { recursive: true })

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2',
  '.json': 'application/json',
}
/** True only for a real regular file — a DIRECTORY must fall through to the SPA
 *  shell (dist/ contains a real `apps/` dir that would otherwise shadow a route
 *  and render blank). Same guard as capture-apps.mjs. */
const isFile = (p) => { try { return statSync(p).isFile() } catch { return false } }

const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  // /logo.png is gateway-served branding, absent from dist.
  if (path === '/logo.png') {
    res.writeHead(200, { 'content-type': 'image/png' })
    res.end(readFileSync(join(DIST, 'icon-192.png')))
    return
  }
  let file = resolve(DIST, '.' + path)
  if (!file.startsWith(resolve(DIST) + sep) && file !== resolve(DIST)) {
    res.writeHead(403); res.end(); return
  }
  if (path === '/' || !isFile(file)) file = join(DIST, 'index.html')  // SPA fallback
  try {
    const body = readFileSync(file)
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' })
    res.end(body)
  } catch {
    res.writeHead(404); res.end()
  }
})

const REPO = { owner: 'kirodotdev', repo: 'KiroCrew' }
const ISO = '2026-08-01T18:00:00Z'

/** Two open PRs, so a bulk selection is genuinely two rows.
 *
 * `head_sha` is on the LIST row because a bulk approve pins each verdict to the
 * commit its row was rendered at — without it the app disables bulk approve, which
 * is exactly the state screenshot 04 would then be showing. */
const PULLS = [
  {
    number: 1111, title: 'feat(issue-radar): act on pull requests from the app',
    url: 'https://github.com/kirodotdev/KiroCrew/pull/1111', state: 'open', draft: false,
    labels: ['enhancement'], author: 'bolinchen', author_association: 'MEMBER',
    created_at: ISO, updated_at: ISO, closed_at: null, merged_at: null,
    assignees: [], requested_reviewers: ['kyleseaman'],
    base: 'main', head: 'feat/pr-actions', head_sha: 'a1b2c3d4e5f6',
    additions: 3106, deletions: 41, changed_files: 22,
    checks_state: 'success',
    checks_counts: { failure: 0, running: 0, success: 12, other: 0 },
    body: 'Adds the write half of the PR pane.',
  },
  {
    number: 1102, title: 'fix(nav): keep the rail width across a reload',
    url: 'https://github.com/kirodotdev/KiroCrew/pull/1102', state: 'open', draft: false,
    labels: ['bug'], author: 'kyleseaman', author_association: 'MEMBER',
    created_at: ISO, updated_at: ISO, closed_at: null, merged_at: null,
    assignees: [], requested_reviewers: [],
    base: 'main', head: 'fix/rail-width', head_sha: '9f8e7d6c5b4a',
    additions: 18, deletions: 4, changed_files: 2,
    checks_state: 'running',
    checks_counts: { failure: 0, running: 3, success: 6, other: 0 },
    body: 'Persist the rail width.',
  },
]

/** The detail read. `mergeable_state` is the whole merge-gate story, so it is the
 *  one field the scenario varies. */
const detail = (over = {}) => ({
  ...PULLS[0],
  merged: false,
  merged_by: null,
  comments: 7, review_comments: 24, commits: 1,
  mergeable: true, mergeable_state: 'clean',
  labels: [{ name: 'enhancement', color: 'a2eeef', description: '' }],
  milestone: null,
  auto_merge: null,
  ...over,
})

const scene = { mergeableState: 'clean', theme: 'dark' }

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

const unmatched = new Set()

async function main() {
  await new Promise((r) => server.listen(PORT, '127.0.0.1', r))
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1680, height: 950 },
    // The subject is a row of small buttons and a 12px composer hint, so 2x — a
    // 1x full-page shot renders the labels illegible on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const IR = '/api/apps/issue-radar'

    if (path === `${IR}/repos`) {
      return json(route, {
        repos: [{
          ...REPO, provider: 'github', host: 'github.com',
          slug: 'kirodotdev/KiroCrew',
          permissions: { triage: true, push: true, maintain: false, admin: false },
        }],
      })
    }
    if (path === `${IR}/me`) return json(route, { login: 'bolinchen' })
    if (path === `${IR}/pulls` || path === `${IR}/pulls/search`) {
      // `bulk_max` is what the client chunks on — omitting it would put the bar in
      // its conservative-fallback path rather than the shipped one.
      return json(route, { ...REPO, pulls: PULLS, from_cache: false, bulk_max: 50 })
    }
    if (path === `${IR}/pull`) {
      return json(route, {
        ...REPO, number: 1111,
        detail: detail({ mergeable_state: scene.mergeableState }),
        timeline: [], checks: [], from_cache: false,
      })
    }
    if (path === `${IR}/pull/runs`) return json(route, { ...REPO, number: 1111, runs: [] })
    if (path === `${IR}/labels`) {
      return json(route, {
        ...REPO, labels: [
          { name: 'enhancement', color: 'a2eeef', description: '' },
          { name: 'bug', color: 'd73a4a', description: '' },
        ],
      })
    }
    // Members are OBJECTS ({login, role}), not bare logins, and settings is the
    // full RepoSettings document — the shapes the app destructures and filters.
    if (path === `${IR}/members`) {
      return json(route, {
        ...REPO, from_cache: false, source: 'collaborators',
        members: [
          { login: 'bolinchen', role: 'write' },
          { login: 'kyleseaman', role: 'admin' },
        ],
      })
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
    if (path === `${IR}/pull-ai` || path === `${IR}/issue-ai`) return json(route, {})
    if (path === `${IR}/recent-repos`) return json(route, { repos: [] })
    if (path === `${IR}/tagging`) {
      return json(route, { ...REPO, issues: [], suggestions: [], titles: [], bulk_max: 50 })
    }
    if (path === `${IR}/recommendations`) return json(route, { ...REPO, recommendations: [] })
    if (path === `${IR}/investigation`) return json(route, { ...REPO, record: null })
    if (path.startsWith(`${IR}/`)) return json(route, {})

    // Dashboard shell.
    if (path === '/api/status') return json(route, { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    // A LIST, like capture-apps.mjs — the shell filters it.
    if (path === '/api/apps') {
      return json(route, [{
        name: 'issue-radar', displayName: 'Issue Radar', enabled: true, builtin: true,
        version: '0.1.0',
        ui: { pages: [{ route: '/issue-radar', label: 'Issue Radar', icon: 'Radar' }] },
      }])
    }
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    // `language` is authoritative over the localStorage fast-path; a payload
    // without it reverts the UI to English mid-boot.
    if (path === '/api/theme/boot') {
      return json(route, { mode: scene.theme, theme: '', language: 'en' })
    }
    if (path.startsWith('/api/effort-levels')) return json(route, ['low', 'medium', 'high', 'xhigh', 'max'])
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/chat/slots') return json(route, [])
    // Object-shaped singletons the shell DESTRUCTURES — a bare [] fallback here
    // throws "instances is not iterable" and the whole page renders the error
    // boundary instead of Issue Radar.
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/agents') {
      return json(route, {
        agents: [{ name: 'default', kiro_agent: 'kirocrew', description: 'Default crew agent' }],
        default_agent: 'default',
      })
    }
    if (path.startsWith('/api/agents/detail/')) return json(route, { name: 'kirocrew', model: 'claude-opus-5', skills: [] })
    if (path === '/api/agents/installed') return json(route, [])
    // A bare LIST: the shell calls .filter() on it directly (App.tsx:767).
    if (path === '/api/approvals') return json(route, [])
    if (path === '/api/terminal/sessions') return json(route, { sessions: [] })
    if (path === '/api/sessions/usage') return json(route, { sessions: [] })
    // The full shape from capture-apps.mjs — the shell reads `operation.status`.
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
    unmatched.add(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 900)))
  page.on('console', (m) => {
    if (m.type() === 'error') console.log('CONSOLE:', m.text().slice(0, 500))
  })

  /** Land directly on the PR list with #1111 selected: the surface under test is
   *  the ACTION bar, and clicking through the dashboard first would only add
   *  scenery to every shot. */
  async function load(theme) {
    scene.theme = theme
    await page.addInitScript((t) => {
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-theme-mode', t)
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('kc:issue-radar:active-repo', JSON.stringify({
        owner: 'kirodotdev', repo: 'KiroCrew', provider: 'github', host: 'github.com',
      }))
      localStorage.setItem('kc:issue-radar:ui-state', JSON.stringify({
        mainView: 'pulls', prStateFilter: 'open', prSelectedPull: 1111,
      }))
    }, theme)
    await page.goto(`http://127.0.0.1:${PORT}/issue-radar`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3000)
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Click a PR row by its number, so the detail pane (and its action bar) opens. */
  async function openPull(number) {
    const row = page.getByText(new RegExp(`#${number}\\b`)).first()
    if (await row.count()) {
      await row.click().catch(() => {})
      await page.waitForTimeout(1800)
    }
  }

  async function clickByName(re) {
    const btn = page.getByRole('button', { name: re }).first()
    if (await btn.count()) {
      await btn.click().catch(() => {})
      await page.waitForTimeout(900)
      return true
    }
    return false
  }

  // 1. The per-PR bar on a PR the provider reports mergeable.
  scene.mergeableState = 'clean'
  await load('dark')
  await openPull(1111)
  await shot('01-pr-actions-bar')

  // 2. The composer. A verdict does not fire on click — "request changes" without a
  //    reason is refused by the provider anyway, and an approval is worth a sentence.
  if (await clickByName(/^approve$/i)) await shot('02-review-composer')

  // 3. A BLOCKED PR. `mergeable` means only "no conflicts", so this PR is
  //    mergeable:true / mergeable_state:"blocked" — no Merge button, auto-merge is
  //    the affordance that lets the provider decide once its checks pass.
  scene.mergeableState = 'blocked'
  await load('dark')
  await openPull(1111)
  await shot('03-merge-blocked')

  // 4/5. The bulk bar and its typed confirmation, over a two-row selection.
  scene.mergeableState = 'clean'
  await load('dark')
  const checkboxes = page.locator('input[type="checkbox"]')
  const n = await checkboxes.count()
  for (let i = 0; i < Math.min(n, 2); i++) {
    await checkboxes.nth(i).click().catch(() => {})
    await page.waitForTimeout(400)
  }
  await page.waitForTimeout(900)
  await shot('04-bulk-bar')
  if (await clickByName(/^close$/i)) await shot('05-bulk-close-confirm')

  // 6. Light theme, so the PR reviewer sees both.
  await load('light')
  await openPull(1111)
  await shot('06-pr-actions-bar-light')

  console.log('unmatched /api paths:', [...unmatched].join(', ') || 'none')
  await context.close()
  await browser.close()
  server.close()
  console.log('done')
}

main()
