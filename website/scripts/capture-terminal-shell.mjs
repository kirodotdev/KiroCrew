/**
 * Screenshot harness for the terminal Default shell setting.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures via Playwright route
 * interception. No gateway, no dashboard token, no PTY spawned — the client
 * code is unmodified, only the network is stubbed, so Settings → Display →
 * Terminal lays out exactly as it does in production.
 *
 * Captured:
 *   01-terminal-card-empty-dark.png    the Terminal card with the new field, unset
 *   02-terminal-card-saved-dark.png    a configured shell persisted and read back
 *   03-terminal-card-error-dark.png    the backend executable-check rejection
 *                                      surfaced inline, draft kept
 *   04-terminal-card-saved-light.png   light-theme parity of the saved state
 *
 * Usage: node scripts/capture-terminal-shell.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-shell'
const VIEW = { width: 1500, height: 950 }

mkdirSync(OUT, { recursive: true })

/** Flipped per-scenario: what the config reads back, and how PATCH answers. */
const scene = { shell: '', theme: 'dark', rejectPatch: false }

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEW,
    // Settings rows are 12–13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()

    if (path === '/api/config/kirocrew' && method === 'PATCH') {
      const body = JSON.parse(route.request().postData() || '{}')
      if (body.path === 'dashboard.terminal.shell') {
        if (scene.rejectPatch) {
          return json(route, {
            error: 'must be an executable shell (an absolute path or a command ' +
              'on PATH); leave empty to use the system default',
            code: 'shell_not_executable',
          }, 400)
        }
        scene.shell = body.value
      }
      return json(route, { ok: true })
    }
    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: { model: 'auto', reasoning_effort: '' },
        dashboard: { terminal: { enabled: true, shell: scene.shell } },
      })
    }
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
    if (path === '/api/chat/nav/resolve-links') return json(route, { summaries: [] })
    if (path === '/api/status') return json(route, { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: scene.theme, theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [] })
    if (path === '/api/dashboard/config') return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  async function load(theme = 'dark') {
    scene.theme = theme
    await page.addInitScript(t => {
      localStorage.clear()
      localStorage.setItem('mc-theme', t)
      localStorage.setItem('mc-onboarded', '1')
    }, theme)
    await page.goto(base + '/settings?tab=display', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2400)
  }

  /** Tight crop around the Terminal card — heading plus its three rows and any
   *  inline error, derived from real boxes so the shot never clips. */
  async function card(name, pad = 20) {
    const heading = page.getByRole('heading', { name: 'Terminal', exact: true })
    const rows = page.locator(
      '[data-setting-label="Font"], [data-setting-label="Font size"], ' +
      '[data-setting-label="Default shell"]',
    )
    const error = page.getByText(/Not an executable shell/)
    if (await heading.count() && await rows.count()) {
      const hb = await heading.first().boundingBox()
      const boxes = []
      for (let i = 0; i < await rows.count(); i++) boxes.push(await rows.nth(i).boundingBox())
      if (await error.count()) boxes.push(await error.first().boundingBox())
      const valid = boxes.filter(Boolean)
      if (hb && valid.length) {
        const x0 = Math.max(0, Math.min(hb.x, ...valid.map(b => b.x)) - pad)
        const y0 = Math.max(0, hb.y - pad)
        const x1 = Math.max(hb.x + hb.width, ...valid.map(b => b.x + b.width)) + pad
        const y1 = Math.max(...valid.map(b => b.y + b.height)) + pad
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: { x: x0, y: y0, width: Math.min(VIEW.width - x0, x1 - x0), height: y1 - y0 },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png (full-page fallback)`)
  }

  const field = () => page.getByLabel('Default shell')

  // 1. Unset: the field is empty, the description carries the guidance.
  scene.shell = ''; scene.rejectPatch = false
  await load('dark')
  await field().scrollIntoViewIfNeeded()
  await card('01-terminal-card-empty-dark')

  // 2. A configured shell, typed + blurred, accepted and read back.
  await field().fill('/usr/bin/fish')
  await field().blur()
  await page.waitForTimeout(900)
  await card('02-terminal-card-saved-dark')

  // 3. A typo: the backend's 400 surfaces inline and the draft survives.
  scene.rejectPatch = true
  await field().fill('/opt/no-such-shell')
  await field().blur()
  await page.waitForTimeout(900)
  await card('03-terminal-card-error-dark')

  // 4. Light-theme parity of the saved state.
  scene.shell = '/usr/bin/fish'; scene.rejectPatch = false
  await load('light')
  await field().scrollIntoViewIfNeeded()
  await card('04-terminal-card-saved-light')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
