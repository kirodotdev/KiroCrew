/**
 * Screenshot harness for the Apps Library Enabled and Disabled sections.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with SPA fallback, and answers every /api/** call from fixtures via
 * Playwright route interception. No gateway, no dashboard token, no kiro-cli
 * spawn — only the network is stubbed, so LibraryPage, useAppsData and the
 * launchpad grid run exactly as they do in production.
 *
 * Three scenes:
 *   1. dark theme — enabled apps first, then the Disabled section below with
 *      greyscale tiles and their Enable buttons. No Open button and no pin
 *      badge on any tile.
 *   2. the same page in the light theme, so the theme-variable claim is
 *      checked rather than asserted.
 *   3. a fresh install where nothing is enabled — "No enabled apps" over the
 *      Disabled section, which still holds every app.
 *   4. a recording of a keyboard Enable: the tile travels from the Disabled
 *      section into the Enabled one, and focus lands on its face.
 *
 * Usage: node scripts/capture-apps-library-sections.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/apps-library-sections'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** One installed-app record in the shape GET /api/apps returns. */
const app = (name, displayName, icon, enabled, origin = 'registry') => ({
  name, displayName, version: '1.0.0', enabled,
  installedAt: '2026-07-01T00:00:00Z', origin, resources: 'gateway', lifecycle: 'gateway',
  manifest: {
    name, version: '1.0.0', displayName,
    description: `${displayName} does things.`, author: 'kirocrew', tags: [name],
    ui: { pages: [{ route: `/${name}-ui`, label: displayName, icon }] },
  },
})

const ENABLED = [
  app('agent-worlds', 'Agent Worlds', 'Globe', true),
  app('research-lab', 'Research Lab', 'FlaskConical', true),
  app('aws-control', 'AWS Control', 'Cloud', true, 'builtin'),
  app('code-review-sage', 'Code Review Sage', 'SearchCode', true),
  app('design-critique', 'Design Critique', 'PenTool', true),
  app('dev-fleet', 'Dev Fleet', 'Server', true),
  app('files', 'Files', 'Files', true, 'builtin'),
  app('issue-radar', 'Issue Radar', 'Radar', true),
]
const DISABLED = [
  app('weather', 'Weather', 'CloudSun', false, 'builtin'),
  app('mail-digest', 'Mail Digest', 'Mail', false, 'builtin'),
  app('kanban', 'Kanban', 'KanbanSquare', false),
  app('standup-timer', 'Standup Timer', 'Timer', false, 'builtin'),
]

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function openLibrary(browser, base, { theme, installed, video }) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 860 },
    // 1x keeps every frame under 2000px on both edges, the cap image readers
    // accept.
    deviceScaleFactor: 1,
    ...(video ? { recordVideo: { dir: OUT, size: { width: 1440, height: 860 } } } : {}),
  })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/apps') return json(route, installed)
    const enable = path.match(/^\/api\/apps\/([^/]+)\/enable$/)
    if (enable && route.request().method() === 'POST') {
      // Enabling flips the stored row, so the shell's refetch sees it on.
      const row = installed.find(a => a.name === enable[1])
      if (row) row.enabled = true
      return json(route, { ok: true })
    }
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
    if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
    if (path === '/api/dashboard/config') return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary|ui-prefs)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })
  await page.addInitScript(t => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
  }, theme)
  await page.goto(base + '/apps/library', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('library-section-disabled').waitFor({ timeout: 15000 })
  return { context, page }
}

async function capture(browser, base, { file, theme, installed, waitFor }) {
  const { context, page } = await openLibrary(browser, base, { theme, installed })
  await waitFor(page)
  await page.waitForTimeout(400)
  const path = `${OUT}/${file}`
  await page.screenshot({ path })
  console.log('wrote', path)
  await context.close()
}

/** Record a keyboard Enable and assert focus followed the tile. */
async function recordEnable(browser, base) {
  const installed = structuredClone([...DISABLED, ...ENABLED])
  const { context, page } = await openLibrary(browser, base, { theme: 'dark', installed, video: true })
  await page.getByTestId('library-section-enabled').waitFor({ timeout: 15000 })
  await page.waitForTimeout(800)
  const enable = page.getByTestId('launchpad-tile-weather').getByRole('button', { name: 'Enable' })
  await enable.focus()
  await page.waitForTimeout(600)
  await page.keyboard.press('Enter')
  const face = page.getByTestId('library-section-enabled')
    .getByTestId('launchpad-tile-weather').getByRole('button', { name: 'Weather', exact: true })
  await face.waitFor({ timeout: 15000 })
  await page.waitForTimeout(1200)
  const focused = await face.evaluate(el => el === document.activeElement)
  if (!focused) throw new Error('focus did not follow the enabled tile to its face')
  const video = page.video()
  await context.close()
  const path = `${OUT}/04-enable-moves-tile.webm`
  await video.saveAs(path)
  await video.delete()
  console.log('wrote', path, '(focus followed the tile)')
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const both = page => page.getByTestId('library-section-enabled').waitFor({ timeout: 15000 })
  await capture(browser, base, { file: '01-sections-dark.png', theme: 'dark', installed: [...DISABLED, ...ENABLED], waitFor: both })
  await capture(browser, base, { file: '02-sections-light.png', theme: 'light', installed: [...DISABLED, ...ENABLED], waitFor: both })
  await capture(browser, base, {
    file: '03-nothing-enabled.png', theme: 'dark', installed: DISABLED,
    waitFor: page => page.getByText('No enabled apps').waitFor({ timeout: 15000 }),
  })
  await recordEnable(browser, base)
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
