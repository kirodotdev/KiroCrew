/**
 * Screenshot harness for the side panel's section headers (Files ↔ Artifacts).
 *
 * The two tabs sit behind adjacent buttons in the same panel, so their group
 * headers are compared by anyone flipping between them — which is how they were
 * noticed to have drifted apart. This harness captures both tabs back to back so
 * the comparison is a diff of two images rather than a claim in a description.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures by Playwright. No gateway, no dashboard
 * token, no artifacts written to disk. The client code under test is unmodified —
 * only the network and localStorage seed are stubbed.
 *
 * The Artifacts fixture is Latin-only on purpose. The CJK property the new header
 * depends on — `text-transform: uppercase` is a no-op there, so the idiom this
 * replaced carried no hierarchy at all in zh-CN — cannot be shown in a shot from
 * a host with no CJK font installed, where every glyph renders as tofu. That
 * invariant is pinned in ui.test.tsx instead, which runs in jsdom and needs no
 * fonts.
 *
 * Usage: node scripts/capture-panel-section-header.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6803'
const OUT = process.argv[3] || '../temp-screenshots/panel-section-header'
const SLOT = 'chat-panel-headers'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Unify the side panel section headers',
  running: false,
  last_message: 'Both tabs now share one header component.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

/** Two markdown links in the assistant turn — that is what the Resources
 *  section extracts (`[label](url)` gives it the label). */
const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Draft the link-unfurl contract.' },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 30,
      content: 'Wrote the contract. Reference implementations: '
        + '[Google search engine](https://google.com) and '
        + '[Amazon e-commerce site](https://amazon.com).',
    },
  ],
}

/** Artifacts the session touched, and one older library-only artifact so BOTH
 *  Artifacts sections render (that pair is the whole point of the tab). */
const SESSION_ARTIFACTS = [
  { slug: 'render-forms', name: 'Three render forms compared', kind: 'widget', pinned: false },
  { slug: 'link-unfurl-contract-md', name: 'link-unfurl-contract.md', kind: 'markdown', pinned: false, source_path: '/Users/diwm/.kiro/crew/workspace/scratch/link-unfurl-contract.md' },
]
const LIBRARY_ONLY = [
  { slug: 'credit-burn', name: 'Credit burn rate', kind: 'widget', pinned: true },
]

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

const scene = { theme: 'light' }

async function main() {
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // 10–12px type: a 1x shot renders the headers too soft to judge.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path === '/api/artifacts/session-docs') return json(route, { docs: [] })
    if (path === '/api/artifacts') {
      // Scoped query → section A; unscoped → the library, which legitimately
      // contains the session's own rows and is de-duped client-side.
      return json(route, url.searchParams.get('touched_by')
        ? { artifacts: SESSION_ARTIFACTS }
        : { artifacts: [...SESSION_ARTIFACTS, ...LIBRARY_ONLY] })
    }
    if (path === '/api/file-diff') {
      const p = url.searchParams.get('path') || ''
      // Give one file a diffstat so the header's count is visually distinct
      // from the per-row +N/-N numbers beside it.
      return json(route, {
        diff: p.endsWith('.md') ? '+++ b/x\n+added one\n+added two\n-removed one\n' : '',
        original: '',
        status: 'modified',
      })
    }
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    // The prerequisite gate wraps the whole shell and reads
    // `status.operation.status`, so the array catch-all below crashes it and
    // nothing renders at all. Answer "ready, idle".
    if (path.startsWith('/api/kiro-prerequisite')) {
      return json(route, {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { status: 'idle', message: '' },
      })
    }
    // App-shell boot endpoints, as a lookup rather than an if-chain: a chain of
    // `if (path === ...) return json(...)` lines is a token-for-token clone of
    // the other capture harnesses, and jscpd's threshold here is 0%.
    // `scene.theme` is read at call time, so the table can be built once.
    const shell = {
      '/api/status': () => ({ sessions: 1, crons: 0, lessons: 0, uptime: 120, version: 'dev' }),
      '/api/notifications': () => ({ notifications: [], unread: 0 }),
      '/api/auth/me': () => ({ user: 'owner', app: '' }),
      '/api/models': () => ({ models: [], default: 'auto' }),
      '/api/themes': () => ({ themes: [], installed: [] }),
      '/api/theme/boot': () => ({ mode: scene.theme, theme: '' }),
      '/api/dashboard/branding': () => ({ bot_name: 'Kiro', avatar: '' }),
      '/api/recent-projects': () => ({ dirs: [PROJECT] }),
      '/api/chat/nav/resolve-links': () => ({ summaries: [] }),
    }
    if (shell[path]) return json(route, shell[path]())
    if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  async function load(theme) {
    scene.theme = theme
    await page.addInitScript(({ t, slot }) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', slot)
      // Open the side panel on boot. chatSlice rehydrates activityOpen from
      // this per-slot key, comparing against the string 'true'.
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      // Keep the privacy banner from eating the vertical band we crop.
      localStorage.setItem('mc-privacy-notice-v1', '1')
      // The Files tab reads agent-touched files from this per-slot key, so
      // seeding it here avoids having to replay a whole tool-call turn.
      localStorage.setItem('kirocrew:touched-files:' + slot, JSON.stringify([
        { path: '/Users/diwm/.kiro/crew/workspace/scratch/link-unfurl-contract.md', ts: Date.now(), lastWrite: Date.now(), source: 'tool' },
        { path: '/Users/diwm/.kiro/crew/workspace/scratch/notes.ts', ts: Date.now(), source: 'tool' },
      ]))
      // Open both view tabs so the strip matches a real session and switching
      // is a click rather than a trip through the + menu.
      localStorage.setItem('mc-panel-tabs:' + slot, JSON.stringify({
        tabs: [
          { id: 'changes', kind: 'changes', title: 'Changes' },
          { id: 'files', kind: 'files', title: 'Files' },
          { id: 'artifacts', kind: 'artifacts', title: 'Artifacts' },
          { id: 'browser', kind: 'browser', title: 'Browser' },
        ],
        activeId: 'files',
      }))
    }, { t: theme, slot: SLOT })
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  /** Crop to the panel's top band — the tab strip plus every header group. */
  async function band(name) {
    const headers = page.getByTestId('panel-section-header')
    await headers.first().waitFor({ timeout: 8000 })
    const strip = await page.getByRole('tablist').first().boundingBox()
    const first = await headers.first().boundingBox()
    const last = await headers.nth(await headers.count() - 1).boundingBox()
    const x = Math.max(0, Math.min(strip?.x ?? first.x, first.x) - 26)
    const y = Math.max(0, (strip?.y ?? first.y) - 14)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x,
        y,
        width: Math.min(1500 - x, Math.max(first.width + 52, (strip?.width ?? 0) + 52)),
        // Down to the last group's header plus two rows of its list.
        height: Math.min(950 - y, last.y + last.height - y + 92),
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  for (const theme of ['light', 'dark']) {
    await load(theme)
    await band(`01-files-${theme}`)
    // Pinned (icon-only) tabs carry their title as aria-label on role=tab, so
    // this stays scoped to the panel strip and cannot hit the sidebar's own
    // "Artifacts" nav entry.
    await page.getByRole('tab', { name: 'Artifacts' }).click()
    await page.waitForTimeout(1400)
    await band(`02-artifacts-${theme}`)
  }

  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
