/**
 * Screenshot harness for the Apps Library "enabled only" default view and its
 * "Show N disabled" reveal control.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with SPA fallback, and answers every /api/** call from fixtures via
 * Playwright route interception. No gateway, no dashboard token, no kiro-cli
 * spawn — only the network is stubbed, so LibraryPage, useAppsData and the
 * launchpad grid run exactly as they do in production.
 *
 * Two scenes, and the second is the one a reviewer cannot infer from the first:
 *   1. default view — enabled apps only, with the labelled reveal control
 *      reading "Show 1 disabled app".
 *   2. the same page after clicking the control — the disabled builtin
 *      revealed and reachable, its Enable verb in the tile menu.
 *
 * The installed set is one enabled third-party app (shown in both scenes) and
 * one disabled builtin (hidden by the default view, revealed on click) so the
 * control's count resolves to exactly one.
 *
 * Usage: node scripts/capture-apps-library-enabled-filter.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/apps-library-enabled-filter'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** The installed apps GET /api/apps returns: one enabled third-party, one
 *  disabled builtin. The disabled builtin is what the default view hides and
 *  the reveal control reveals. */
const INSTALLED = [
  {
    name: 'oncall-radar', displayName: 'Oncall Radar', version: '1.0.0', enabled: true,
    installedAt: '2026-07-01T00:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name: 'oncall-radar', version: '1.0.0', displayName: 'Oncall Radar',
      description: 'Watches your on-call rotations.', author: 'zezhexu', tags: ['oncall'],
      ui: { pages: [{ route: '/oncall-radar-ui', label: 'Oncall Radar', icon: 'Bot' }] },
    },
  },
  {
    name: 'aws-control', displayName: 'AWS Control', version: '1.0.0', enabled: false,
    installedAt: '2026-06-01T00:00:00Z', origin: 'builtin', resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name: 'aws-control', version: '1.0.0', displayName: 'AWS Control',
      description: 'Manage AWS accounts and access.', author: 'kirocrew', tags: ['aws'],
      ui: { pages: [{ route: '/aws-control', label: 'AWS Control', icon: 'Cloud' }] },
    },
  },
]

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // Tile captions and the control are 12–13px type; a 1x shot renders soft
    // on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname

    if (path === '/api/apps') return json(route, INSTALLED)
    if (path === '/api/apps/registry') return json(route, { apps: [], categoryOrder: [], editorialSections: [] })
    if (path === '/api/apps/registries') return json(route, { registries: [] })
    // The app shell mounts behind this gate and reads status.operation.status —
    // the generic object stub crashes it, blanking the whole page.
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
    if (path === '/api/status') return json(route, { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
    if (path === '/api/dashboard/config') return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary|ui-prefs)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
  })
  await page.goto(base + '/apps/library', { waitUntil: 'domcontentloaded' })

  // Scene 1: the shipped default. The enabled app is a tile; the disabled
  // builtin is filtered, and the reveal control names the hidden count.
  const reveal = page.getByRole('button', { name: 'Show 1 disabled app' })
  await reveal.waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/01-default-enabled-only.png` })
  console.log('wrote', `${OUT}/01-default-enabled-only.png`)

  // Scene 2: after clicking the control — the disabled builtin is revealed and
  // reachable, the control now offers the way back.
  await reveal.click()
  await page.getByTestId('launchpad-tile-aws-control').waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: 'Show enabled only' }).waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/02-show-all-revealed.png` })
  console.log('wrote', `${OUT}/02-show-all-revealed.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
