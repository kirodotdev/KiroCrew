/**
 * Screenshot harness for the app uninstall confirm dialog's dependency
 * classification panel (#10880).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with SPA fallback, and answers every /api/** call from fixtures via
 * Playwright route interception — including the newly registered
 * GET /api/apps/{name}/uninstall/preview, whose payload is what the panel
 * renders. Before the route existed, that fetch 404'd and the panel never
 * rendered for anyone; this scene is the first time it appears.
 *
 * One scene: the uninstall confirm dialog for an installed app, showing all
 * three dependency classes — removable (deleted with the app, keepable via
 * checkbox), shared (kept, used by another app), user-installed (kept).
 *
 * Usage: node scripts/capture-apps-uninstall-preview.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/apps-uninstall-preview'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const INSTALLED = [
  {
    name: 'oncall-radar', displayName: 'Oncall Radar', version: '1.0.0', enabled: true,
    installedAt: '2026-07-01T00:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name: 'oncall-radar', version: '1.0.0', displayName: 'Oncall Radar',
      description: 'Watches your on-call rotations.', author: 'zezhexu', tags: ['oncall'],
      agents: ['radar-agent'],
      skills: ['skills/mochi-slack'],
      crons: [{ name: 'radar-sweep' }],
      ui: { pages: [{ route: '/oncall-radar-ui', label: 'Oncall Radar', icon: 'Bot' }] },
    },
  },
]

/** What the newly registered route answers: one dependency in each class, so
 *  the panel shows every row style it has. */
const PREVIEW = {
  app: 'oncall-radar',
  lifecycle: 'gateway',
  resources: { agents: ['radar-agent'], skills: ['skills/mochi-slack'], crons: ['radar-sweep'] },
  dependencies: {
    removable: [{ id: 'skills/mochi-slack', type: 'skill', reason: 'installed with this app' }],
    shared: [{ id: 'skills/widgets', type: 'skill', usedBy: ['pets'], reason: 'also used by Pets' }],
    userInstalled: [{ id: 'skills/grill', type: 'skill', reason: 'installed by you' }],
  },
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname

    if (path === '/api/apps/oncall-radar/uninstall/preview') return json(route, PREVIEW)
    if (path === '/api/apps') return json(route, INSTALLED)
    if (path === '/api/apps/registry') return json(route, { apps: [], categoryOrder: [], editorialSections: [] })
    if (path === '/api/apps/registries') return json(route, { registries: [] })
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

  // Open the tile's overflow menu, then Uninstall — same path a user takes.
  const tile = page.getByTestId('launchpad-tile-oncall-radar')
  await tile.waitFor({ timeout: 15000 })
  await tile.hover()
  const menu = page.getByRole('button', { name: 'More actions for Oncall Radar' })
  await menu.waitFor({ timeout: 15000 })
  // The tile overlay intercepts hit-testing until hover styles settle; the
  // button is real and visible, so a forced click is safe here.
  await menu.click({ force: true })
  await page.getByRole('menuitem', { name: 'Uninstall' }).click()

  // The dialog with the dependency panel rendered from the preview payload.
  const dialog = page.getByRole('dialog', { name: 'Confirm uninstall' })
  await dialog.waitFor({ timeout: 15000 })
  await dialog.getByText('installed with this app').waitFor({ timeout: 15000 })
  await dialog.getByText('also used by Pets').waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/01-uninstall-dialog-dependency-panel.png` })
  console.log('wrote', `${OUT}/01-uninstall-dialog-dependency-panel.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
