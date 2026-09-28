/**
 * Screenshot harness for the source-tab project qualifier (#9720).
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures. The scene: one chat referencing two GitLab merge requests from two
 * DIFFERENT projects that share the IID `!1` — exactly the collision the issue
 * reports. Before the fix both tabs read `MR !1`; after it they read
 * `group-a/service MR !1` and `group-b/service MR !1`.
 *
 * A second scene renders two MRs from the SAME project to show the concise
 * bare labels are unchanged.
 *
 * Usage: node scripts/capture-mr-tab-project-label.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6801'
const OUT = process.argv[3] || '../temp-screenshots/mr-tab-project-label'
const SLOT = 'chat-mr-tabs'
const MR_A = 'https://gitlab.com/group-a/service/-/merge_requests/1'
const MR_B = 'https://gitlab.com/group-b/service/-/merge_requests/1'
const MR_A2 = 'https://gitlab.com/group-a/service/-/merge_requests/2'
const MR_DEEP_A = 'https://gitlab.com/platform/services/ingest/-/merge_requests/1'
const MR_DEEP_B = 'https://gitlab.com/platform/services/egress/-/merge_requests/1'

mkdirSync(OUT, { recursive: true })

/** Mutable scenario the route handlers read: the pair of MRs on camera. */
const scene = { links: [] }

const slot = () => [{
  key: SLOT,
  title: 'Release the service split',
  running: false,
  last_message: 'Both merge requests are open for review.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: Math.floor(Date.now() / 1000),
  source_links: scene.links.map(url => ({ provider: 'gitlab', number: Number(url.split('/').pop()), url, state: 'open', ci: 'passed' })),
  source_links_total: scene.links.length,
}]

const detail = () => ({
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', content: 'Open the merge requests for the service split', ts: Date.now() / 1000 - 600 },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: `Opened ${scene.links.join(' and ')}.` },
  ],
})

const source = url => ({
  provider: 'gitlab',
  url,
  number: Number(url.split('/').pop()),
  title: url.includes('group-a') ? 'Split the ingestion path' : 'Split the delivery path',
  description: 'One half of the service split.',
  state: 'open',
  draft: false,
  mergedAt: '',
  updatedAt: new Date().toISOString(),
  headBranch: 'feat/split',
  baseBranch: 'main',
  headSha: 'a1b2c3d4',
  author: 'octocat',
  additions: 120,
  deletions: 8,
  changedFiles: 3,
  mergeable: 'mergeable',
  mergeStateStatus: 'clean',
  commits: [{ sha: 'a1b2c3d4', message: 'feat: split the service', author: 'octocat', committedAt: new Date().toISOString(), url }],
  checks: [],
  comments: [],
  files: [{ path: 'service/pipeline.py', status: 'modified', additions: 120, deletions: 8, patch: '' }],
})

const json = (route, body) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 },
    // The story is a 12px tab label: capture at 2x so it reads on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  // Static dashboard-boot endpoints as a lookup table (also keeps this block
  // structurally distinct from sibling capture harnesses for the jscpd gate).
  const staticRoutes = {
    '/api/status': { sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' },
    '/api/notifications': { notifications: [], unread: 0 },
    '/api/config': {},
    '/api/kirocrew-config': {},
    '/api/dashboard/branding': { bot_name: 'Kiro', avatar: '' },
    '/api/auth/me': { user: 'owner', app: '' },
    '/api/models': { models: [], default: 'auto' },
    '/api/themes': { themes: [], installed: [] },
    '/api/theme/boot': { mode: 'dark', theme: '' },
    '/api/chat/nav/resolve-links': { summaries: [] },
    '/api/source/pull-request/checks': { checks: [] },
    // The first-run prerequisite gate covers the whole app until the CLI
    // reports ready; answer it as fully set up so the chat is on camera.
    '/api/kiro-prerequisite': {
      platform: 'Linux', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, repair_required: false,
      docs_url: '', login_command: '', sso_login_command: '',
      setup_allowed: true, sandbox_unavailable: false, sandbox_failure_kind: '',
      sandbox_detail: '', sandbox_remedy: '', missing_agent_specs: [],
      agent_spec_repair_error: '',
    },
  }

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path in staticRoutes) return json(route, staticRoutes[path])
    if (path === '/api/chat/slots') return json(route, slot())
    if (path.startsWith('/api/chat/slots/')) return json(route, detail())
    if (path === '/api/source/pull-request') {
      const target = url.searchParams.get('url') || scene.links[0]
      return json(route, source(target))
    }
    if (path === '/api/source/pull-request/status') {
      return json(route, { statuses: Object.fromEntries(scene.links.map(u => [u, { state: 'open', ci: 'passed' }])), refreshing: [], ttlSecs: 60 })
    }
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 200)))

  async function load() {
    await page.addInitScript(() => {
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-mr-tabs')
    })
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
    const opener = page.getByRole('button', { name: 'Open activity panel' })
    if (await opener.count()) {
      await opener.first().click().catch(() => {})
      await page.waitForTimeout(1200)
    }
    const changes = page.getByRole('button', { name: /^Changes/ })
    if (await changes.count()) {
      await changes.first().click().catch(() => {})
      await page.waitForTimeout(2000)
    }
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Crop holding the panel's source-tab strip. */
  async function strip(name) {
    // The source strip is the tablist labelled by the pull-requests catalog
    // string; `.first()` alone can land on an unrelated (and tiny) tablist.
    const tab = page.getByRole('tab', { name: /MR !/ })
    const count = await tab.count()
    if (count) {
      const first = await tab.first().boundingBox()
      const last = await tab.nth(count - 1).boundingBox()
      if (first && last) {
        const x = Math.max(first.x - 16, 0)
        const y = Math.max(Math.min(first.y, last.y) - 16, 0)
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x, y,
            width: Math.min(last.x + last.width + 16 - x, 1600 - x),
            height: Math.max(first.height, last.height) + 32,
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    await shot(name)
  }

  // 1. The fix on camera: two projects sharing IID !1 → qualified tabs.
  scene.links = [MR_A, MR_B]
  await load()
  await shot('01-two-projects-qualified')
  await strip('01b-two-projects-qualified-strip')

  // 2. Single project: the concise bare labels are unchanged.
  scene.links = [MR_A, MR_A2]
  await load()
  await strip('02-single-project-bare-strip')

  // 3. Deep subgroup paths differing only at the tail: the qualifier keeps
  //    the discriminating segment (`…/ingest` vs `…/egress`) instead of
  //    end-truncating both to an identical prefix.
  scene.links = [MR_DEEP_A, MR_DEEP_B]
  await load()
  await strip('03-deep-paths-unique-tail-strip')

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
